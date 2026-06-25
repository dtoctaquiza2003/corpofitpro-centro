from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..database import Base


class UsuarioPermisoTemporal(Base):
    __tablename__ = "usuarios_permisos_temporales"

    id = Column(Integer, primary_key=True, index=True)

    usuarioid = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    autorizado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    consultorioid = Column(Integer, ForeignKey("consultorios.id"), nullable=True)

    tipo_permiso = Column(String(80), nullable=False)

    fecha_inicio = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
    )
    fecha_fin = Column(DateTime(timezone=True), nullable=False)

    dias_atras_permitidos = Column(Integer, nullable=False, default=0)
    motivo = Column(Text, nullable=True)

    activo = Column(Boolean, nullable=False, default=True)
    fechacreacion = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )

    usuario = relationship(
        "Usuario",
        foreign_keys=[usuarioid],
    )

    autorizado_por = relationship(
        "Usuario",
        foreign_keys=[autorizado_por_id],
    )