"""
database.py — configuración del pool optimizada para:
  • Supabase Transaction Pooler  (puerto 6543)
  • Render Free / Starter (1 instancia, ~512 MB RAM)

Límite duro en Supabase Free: 15 conexiones directas / ~200 en Pooler.
Con workers=1 en uvicorn el pool entero vive en un solo proceso.

Regla de oro para Transaction Pooler:
  pool_size + max_overflow ≤ (límite_pooler / nro_instancias) - margen
  3 + 2 = 5  →  muy seguro para 1 instancia con picos cortos.
"""

import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .core.config import settings


# ---------------------------------------------------------------------------
# Helpers para leer variables de entorno con defaults seguros
# ---------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        print(f"⚠️  {name}={value!r} no es un número válido. Usando {default}.")
        return default


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


# ---------------------------------------------------------------------------
# Configuración de pool
# ---------------------------------------------------------------------------

DATABASE_URL = settings.DATABASE_URL

IS_TRANSACTION_POOLER = (
    ":6543" in DATABASE_URL
    or _env_str("DB_POOL_MODE", "").lower() in {"transaction", "transaccional"}
)

# ⚠️  CRÍTICO: con Transaction Pooler no se pueden usar prepared statements
# ni SET SESSION …, ni advisory locks entre requests, porque cada checkout
# puede ir a un backend PostgreSQL distinto.
# pool_size=3 + max_overflow=2 = 5 conexiones máx. por proceso (seguro).
DB_POOL_SIZE    = _env_int("DB_POOL_SIZE",    3)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 2)

# pool_timeout corto: si el pool está lleno fallamos rápido en vez de
# mantener el request colgado y acumular más peticiones en cola.
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 8)

# Reciclar conexiones cada 3 min evita que Supabase cierre la conexión
# por inactividad (PgBouncer cierra idle > ~10 min por defecto).
DB_POOL_RECYCLE  = _env_int("DB_POOL_RECYCLE",  180)
DB_CONNECT_TIMEOUT = _env_int("DB_CONNECT_TIMEOUT", 10)
DB_SSLMODE       = _env_str("DB_SSLMODE", "require")

APPLICATION_NAME = os.getenv(
    "DB_APPLICATION_NAME",
    os.getenv("RENDER_SERVICE_NAME", "corpofit-api"),
)

connect_args: dict = {
    "application_name": APPLICATION_NAME,
    "connect_timeout": DB_CONNECT_TIMEOUT,
}
if DB_SSLMODE:
    connect_args["sslmode"] = DB_SSLMODE

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    # pool_pre_ping: detecta conexiones muertas ANTES de dárselas al código.
    # Evita "connection closed" en requests largos o tras idle.
    pool_pre_ping=True,
    # LIFO: reutiliza la conexión más reciente → mantiene las "calientes"
    # y permite que las extras caduquen más rápido.
    pool_use_lifo=True,
    # Siempre hacer rollback al devolver la conexión al pool.
    # Evita que una transacción abierta accidentalmente bloquee tablas.
    pool_reset_on_return="rollback",
    echo=False,
    connect_args=connect_args,
    # isolation_level READ COMMITTED es suficiente para la mayoría de ops.
    # Reducir a AUTOCOMMIT solo en endpoints de solo-lectura de alto tráfico
    # si se decide en el futuro (no aquí, requiere cambiar get_db).
)

# ---------------------------------------------------------------------------
# Optimización: statement_timeout por conexión
# Mata queries que tarden más de 25 s en vez de bloquear el worker.
# ---------------------------------------------------------------------------
@event.listens_for(engine, "connect")
def _set_timeouts(dbapi_conn, connection_record):
    with dbapi_conn.cursor() as cur:
        # 25 000 ms = 25 s — ajustar si hay reportes legítimamente lentos
        cur.execute("SET statement_timeout = '25000'")
        # lock_timeout: no esperar más de 5 s para obtener un lock de tabla
        cur.execute("SET lock_timeout = '5000'")


print(
    "🗄️  DB pool config → "
    f"transaction_pooler={IS_TRANSACTION_POOLER}, "
    f"pool_size={DB_POOL_SIZE}, "
    f"max_overflow={DB_MAX_OVERFLOW}, "
    f"pool_timeout={DB_POOL_TIMEOUT}s, "
    f"pool_recycle={DB_POOL_RECYCLE}s"
)

# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    # expire_on_commit=False: los objetos ORM NO se invalidan tras commit,
    # así el código puede leer atributos después de db.commit() sin otro
    # SELECT. Reduce queries innecesarios en endpoints que leen tras guardar.
    expire_on_commit=False,
)

Base = declarative_base()


# ---------------------------------------------------------------------------
# Dependency para FastAPI
# ---------------------------------------------------------------------------

def get_db():
    """
    Dependency que abre una sesión y la cierra al terminar el request.

    Patrón correcto:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()   ← devuelve la conexión al pool (no la destruye)

    NO usar db.commit() dentro de get_db; cada endpoint es responsable
    de hacer commit / rollback explícitamente.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
