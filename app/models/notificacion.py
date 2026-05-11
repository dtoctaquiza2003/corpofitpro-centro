from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from ..database import Base


class Notificacion(Base):
    __tablename__ = "notificaciones"

    id = Column(Integer, primary_key=True, index=True)

    usuarioid = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    titulo = Column(Text, nullable=False)
    mensaje = Column(Text, nullable=False)
    tipo = Column(Text, nullable=False, index=True)

    referencia_tipo = Column(Text, nullable=True)
    referencia_id = Column(Integer, nullable=True)

    leida = Column(Boolean, nullable=False, default=False)

    fecha = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    data = Column(JSONB, nullable=True)