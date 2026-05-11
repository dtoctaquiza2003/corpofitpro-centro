from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.sql import func

from ..database import Base


class UsuarioDispositivo(Base):
    __tablename__ = "usuario_dispositivos"

    id = Column(Integer, primary_key=True, index=True)

    usuarioid = Column(
        Integer,
        ForeignKey("usuarios.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    fcm_token = Column(Text, nullable=False, unique=True, index=True)
    plataforma = Column(Text, nullable=True)

    activo = Column(Boolean, nullable=False, default=True)

    fecha_registro = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    fecha_actualizacion = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )