from pydantic import BaseModel
from typing import Optional
from datetime import date

class ConsultorioBase(BaseModel):
    nombre: str
    direccion: Optional[str] = None
    telefono: Optional[str] = None
    fecha_apertura: Optional[date] = None
    activo: bool = True

class ConsultorioOut(ConsultorioBase):
    id: int

    class Config:
        from_attributes = True