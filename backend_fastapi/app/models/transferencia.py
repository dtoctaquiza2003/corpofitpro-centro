from sqlalchemy import Column, Integer, DateTime, String, Boolean, ForeignKey, Table
from sqlalchemy.orm import relationship
from ..database import Base

# Tabla asociativa (muchos a muchos) entre transferencias y pacientes
transferencia_paciente = Table(
    'transferencia_paciente',
    Base.metadata,
    Column('transferencia_id', Integer, ForeignKey('transferencias.id')),
    Column('paciente_id', Integer, ForeignKey('pacientes.id'))
)

class Transferencia(Base):
    __tablename__ = "transferencias"

    id = Column(Integer, primary_key=True, index=True)
    terapeuta_origen_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    terapeuta_destino_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    fecha_inicio = Column(DateTime, nullable=False)
    activo = Column(Boolean, default=True)
    motivo = Column(String(255), nullable=True)
    fecha_retorno_real = Column(DateTime, nullable=True)

    pacientes = relationship("Paciente", secondary=transferencia_paciente)
    terapeuta_origen = relationship("Usuario", foreign_keys=[terapeuta_origen_id])
    terapeuta_destino = relationship("Usuario", foreign_keys=[terapeuta_destino_id])