from pydantic import BaseModel, Field, field_serializer

from app.utils.fechas import to_ecuador
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

    @field_serializer("fecha")
    def serializar_fecha_ecuador(self, value: datetime, _info):
        return to_ecuador(value) if value is not None else None

    class Config:
        from_attributes = True
        populate_by_name = True
