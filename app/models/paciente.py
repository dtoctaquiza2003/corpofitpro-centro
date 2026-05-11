from sqlalchemy import Column, Computed, Integer, String, Date, DateTime, Boolean, SmallInteger, Text, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from ..database import Base

class Paciente(Base):
    __tablename__ = "pacientes"

    id = Column(Integer, primary_key=True, index=True)
    consultorioid = Column(Integer, ForeignKey("consultorios.id"), nullable=False)
    terapeutaasignadoid = Column(Integer, ForeignKey("usuarios.id"))
    nombres = Column(String(100))
    apellidos = Column(String(100))
    cedula = Column(String(10), unique=True)
    fechanacimiento = Column(Date)
    telefono = Column(String(20))
    direccion = Column(String(255))
    # SEXO, OCUPACION, CORREO, ETC.
    sexo = Column(SmallInteger)
    ocupacion = Column(String(100))
    correoelectronico = Column(String(100))
    tiposeguro = Column(String(100))
    motivoconsulta = Column(String(500))
    examenescomplementarios = Column(Text)
    consentimientofirmado = Column(Boolean, default=False)
    consentimientofecha = Column(DateTime(timezone=True))
    historiaclinicaid = Column(
        String,
        Computed("'HP-' || lpad(id::text, 6, '0')", persisted=True),
        nullable=False,
    )
    
    fechainicio = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    estadopaciente = Column(SmallInteger, default=1)
    fechaalta = Column(Date)

    # Relaciones
    # ... dentro de la clase Paciente, después de las columnas:
    diagnosticos = relationship("Diagnostico", back_populates="paciente", cascade="all, delete-orphan")
    tratamientos_historial = relationship("TratamientoPaciente", back_populates="paciente", cascade="all, delete-orphan")