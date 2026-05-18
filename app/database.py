from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .core.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_size=4,
    max_overflow=2,
    pool_timeout=45,
    pool_recycle=180,
    pool_pre_ping=True,
    echo=False,
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