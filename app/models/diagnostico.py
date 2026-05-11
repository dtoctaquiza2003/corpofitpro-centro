from sqlalchemy import Column, Integer, String, Text, Date, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from ..database import Base

class Diagnostico(Base):
    __tablename__ = "diagnosticos"

    id = Column(Integer, primary_key=True, index=True)
    pacienteid = Column(Integer, ForeignKey("pacientes.id"), nullable=False)
    diagnostico = Column(Text, nullable=False)
    fechadiagnostico = Column(Date, nullable=False)
    activo = Column(Boolean, default=True)
    notas = Column(Text)
    fechacreacion = Column(DateTime(timezone=True), server_default=func.current_timestamp())

    paciente = relationship("Paciente", back_populates="diagnosticos")

    tratamientos = relationship("TratamientoPaciente", back_populates="diagnostico")