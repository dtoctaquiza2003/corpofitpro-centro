from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.sql import func
from ..database import Base

class Alerta(Base):
    __tablename__ = "alertas"

    id = Column(Integer, primary_key=True, index=True)
    paciente_id = Column("pacienteid", Integer, ForeignKey("pacientes.id")) # ← Corrección aquí
    tipo = Column(String(50))
    descripcion = Column(String(500))
    fecha = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    leida = Column(Boolean, default=False)