from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, Text
from sqlalchemy.sql import func

from ..database import Base


class PacienteTerapeutaCompartido(Base):
    __tablename__ = "pacientes_terapeutas_compartidos"

    id = Column(Integer, primary_key=True, index=True)

    pacienteid = Column(
        Integer,
        ForeignKey("pacientes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    terapeutaid = Column(
        Integer,
        ForeignKey("usuarios.id"),
        nullable=False,
        index=True,
    )

    tipoterapiaid = Column(
        Integer,
        ForeignKey("tipos_terapia.id"),
        nullable=True,
    )

    motivo = Column(Text, nullable=True)

    fecha_inicio = Column(Date, nullable=True)
    fecha_fin = Column(Date, nullable=True)

    activo = Column(Boolean, default=True, nullable=False)

    creado_por_id = Column(
        Integer,
        ForeignKey("usuarios.id"),
        nullable=True,
    )

    fechacreacion = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )