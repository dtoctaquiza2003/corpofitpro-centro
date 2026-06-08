from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class AlertaOut(BaseModel):
    id: int
    pacienteid: int = Field(..., alias="paciente_id")
    tipo: str
    tipo_label: Optional[str] = None
    descripcion: str
    fecha: datetime
    leida: bool

    # Datos enriquecidos para mostrar en la app.
    paciente_nombre: Optional[str] = None
    terapeuta_id: Optional[int] = None
    terapeuta_nombre: Optional[str] = None

    class Config:
        from_attributes = True
        populate_by_name = True
