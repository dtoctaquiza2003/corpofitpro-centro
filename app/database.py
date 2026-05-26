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


# CORPOFIT Pro usa 2 servicios Render conectados a la misma BD.
# La fórmula real de conexiones máximas es:
# (DB_POOL_SIZE + DB_MAX_OVERFLOW) * WORKERS * CANTIDAD_DE_RENDERS
#
# Para 2 Render con 1 worker cada uno:
# (4 + 0) * 1 * 2 = 8 conexiones máximas controladas.
#
# Antes tenías pool_size=4 y max_overflow=2:
# (4 + 2) * 1 * 2 = 12 conexiones, demasiado cerca del límite 15
# del pooler session mode de Supabase. En deploy/restart se puede duplicar
# temporalmente y provocar EMAXCONNSESSION.
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 4)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 0)
DB_POOL_TIMEOUT = _env_int("DB_POOL_TIMEOUT", 40)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 180)

APPLICATION_NAME = os.getenv(
    "DB_APPLICATION_NAME",
    os.getenv("RENDER_SERVICE_NAME", "corpofit-api"),
)

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
    pool_use_lifo=True,
    echo=False,
    connect_args={
        "application_name": APPLICATION_NAME,
    },
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
