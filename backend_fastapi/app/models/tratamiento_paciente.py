from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..database import Base


class TratamientoPaciente(Base):
    __tablename__ = "tratamientos_paciente"

    id = Column(Integer, primary_key=True, index=True)

    pacienteid = Column(Integer, ForeignKey("pacientes.id"), nullable=False)
    diagnosticoid = Column(Integer, ForeignKey("diagnosticos.id"), nullable=True)

    # Campo antiguo, se mantiene para compatibilidad visual
    tipotratamiento = Column(String(200), nullable=False)

    # Nuevo flujo
    tipoterapiaid = Column(Integer, ForeignKey("tipos_terapia.id"), nullable=True)
    precio_sesion_oficial = Column(Numeric(10, 2), nullable=True)
    precio_sesion_aplicado = Column(Numeric(10, 2), nullable=True)
    sesiones_estimadas = Column(Integer, nullable=True)
    motivo_precio_especial = Column(String(255), nullable=True)
    multiple_extremidad = Column(Boolean, default=False, nullable=False)

    fechainicio = Column(Date, nullable=False)
    fechafin = Column(Date, nullable=True)

    observaciones = Column(Text, nullable=True)
    activo = Column(Boolean, default=True)

    fechacreacion = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )

    paciente = relationship("Paciente", back_populates="tratamientos_historial")
    diagnostico = relationship("Diagnostico", back_populates="tratamientos")
    tipo_terapia = relationship("TipoTerapia", back_populates="tratamientos")