from sqlalchemy import Column, Integer, String, Float, Boolean
from ..database import Base

class Paquete(Base):
    __tablename__ = "paquetes"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100))
    cantidad_sesiones = Column("cantidadsesiones", Integer)   # ← mapeo
    precio_oficial = Column("preciooficial", Float)           # ← mapeo
    duracion_dias = Column("duraciondias", Integer)           # ← mapeo
    activo = Column(Boolean, default=True)