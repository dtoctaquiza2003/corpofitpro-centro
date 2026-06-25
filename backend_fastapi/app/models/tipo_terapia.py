from sqlalchemy import Boolean, Column, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from ..database import Base


class TipoTerapia(Base):
    __tablename__ = "tipos_terapia"

    id = Column(Integer, primary_key=True, index=True)
    nombre = Column(String(100), nullable=False)
    descripcion = Column(Text, nullable=True)
    precio_sesion = Column(Numeric(10, 2), nullable=False)
    activo = Column(Boolean, default=True)
    fechacreacion = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )

    tratamientos = relationship(
        "TratamientoPaciente",
        back_populates="tipo_terapia",
    )