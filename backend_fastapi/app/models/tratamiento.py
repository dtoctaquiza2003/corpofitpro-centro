from sqlalchemy import Column, Integer, String, Boolean, ForeignKey
from sqlalchemy.orm import synonym

from ..database import Base


class TipoTratamiento(Base):
    __tablename__ = "tipostratamiento"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100))
    activo = Column(Boolean, default=True)


class SesionTratamiento(Base):
    __tablename__ = "sesiontratamiento"

    id = Column(Integer, primary_key=True, index=True)
    sesionid = Column("sesionid", Integer, ForeignKey("sesionesterapia.id"))
    tratamientoid = Column("tratamientoid", Integer, ForeignKey("tipostratamiento.id"))
    intensidad = Column("intensidad", String(50))
    duracionminutos = Column("duracionminutos", Integer)

    sesion_id = synonym("sesionid")
    tratamiento_id = synonym("tratamientoid")
    duracion_minutos = synonym("duracionminutos")