from sqlalchemy import Column, Integer, String, Date, Boolean
from ..database import Base

class Consultorio(Base):
    __tablename__ = "consultorios"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    direccion = Column(String(255))
    telefono = Column(String(20))
    fecha_apertura = Column("fechaapertura", Date)  # ← mapeo explícito
    activo = Column(Boolean, default=True)