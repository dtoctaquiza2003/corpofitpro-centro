from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from .core.config import settings

# Crear el motor de la base de datos
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,       # Verifica conexiones antes de usarlas
    echo=False                # Cambiar a True para ver SQL en consola (debug)
)

# Fábrica de sesiones
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base para los modelos declarativos
Base = declarative_base()

def get_db():
    """
    Dependencia para obtener una sesión de base de datos.
    Se cierra automáticamente al finalizar la petición.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()