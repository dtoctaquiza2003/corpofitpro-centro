import sys
from pathlib import Path

# Agrega la ruta del proyecto al PYTHONPATH
sys.path.append(str(Path(__file__).parent.parent))

from app.core.config import settings
from app.database import Base
from app.models import *  # Importa todos los modelos

from alembic import context

target_metadata = Base.metadata

def run_migrations_offline():
    """Ejecuta migraciones en modo offline (solo genera SQL)."""
    context.configure(url=settings.DATABASE_URL, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    """Ejecuta migraciones en modo online (conecta a la BD)."""
    from sqlalchemy import create_engine
    connectable = create_engine(settings.DATABASE_URL)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()