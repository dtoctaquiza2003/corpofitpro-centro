import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .core.config import settings


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)

    if value is None or value.strip() == "":
        return default

    try:
        return int(value)
    except ValueError:
        print(
            f"⚠️ Variable {name}={value!r} no es un número válido. "
            f"Usando {default}."
        )
        return default


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


DATABASE_URL = settings.DATABASE_URL

# Supabase Transaction Pooler normalmente usa puerto 6543.
# En ese modo se permite un overflow pequeño para absorber picos cortos.
IS_TRANSACTION_POOLER = (
    ":6543" in DATABASE_URL
    or _env_str("DB_POOL_MODE", "").lower() in {"transaction", "transaccional"}
)

DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _env_int(
    "DB_MAX_OVERFLOW",
    2 if IS_TRANSACTION_POOLER else 0,
)

# No conviene dejar 40-45s: Render marca el health check como fallido en 5s.
# Si el pool está ocupado, fallamos rápido y dejamos que la app se recupere.
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 8)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 180)
DB_CONNECT_TIMEOUT = _env_int("DB_CONNECT_TIMEOUT", 10)
DB_SSLMODE = _env_str("DB_SSLMODE", "require")

APPLICATION_NAME = os.getenv(
    "DB_APPLICATION_NAME",
    os.getenv("RENDER_SERVICE_NAME", "corpofit-api"),
)

connect_args = {
    "application_name": APPLICATION_NAME,
    "connect_timeout": DB_CONNECT_TIMEOUT,
}

if DB_SSLMODE:
    connect_args["sslmode"] = DB_SSLMODE

print(
    "🗄️ DB pool config -> "
    f"transaction_pooler={IS_TRANSACTION_POOLER}, "
    f"pool_size={DB_POOL_SIZE}, "
    f"max_overflow={DB_MAX_OVERFLOW}, "
    f"pool_timeout={DB_POOL_TIMEOUT}, "
    f"pool_recycle={DB_POOL_RECYCLE}"
)

engine = create_engine(
    DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
    pool_use_lifo=True,
    pool_reset_on_return="rollback",
    echo=False,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
