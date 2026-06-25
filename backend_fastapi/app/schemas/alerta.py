from pydantic import BaseModel, Field, field_serializer

from app.utils.fechas import to_ecuador
from datetime import datetime
from typing import Any, Optional


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



class SesionAuditoriaAlertaOut(BaseModel):
    id: int
    titulo: str
    mensaje: str
    tipo: str
    leida: bool
    fecha: datetime

    referencia_tipo: Optional[str] = None
    referencia_id: Optional[int] = None

    accion: Optional[str] = None
    sesion_id: Optional[int] = None
    paciente_id: Optional[int] = None
    paciente_nombre: Optional[str] = None
    consultorioid: Optional[int] = None
    consultorio_nombre: Optional[str] = None
    terapeuta_id: Optional[int] = None
    terapeuta_nombre: Optional[str] = None
    secretario_id: Optional[int] = None
    secretario_nombre: Optional[str] = None
    tratamiento_anterior_id: Optional[int] = None
    tratamiento_anterior_nombre: Optional[str] = None
    tratamiento_nuevo_id: Optional[int] = None
    tratamiento_nuevo_nombre: Optional[str] = None
    tratamientos_aplicados: list[str] = Field(default_factory=list)
    data: Optional[dict[str, Any]] = None

    @field_serializer("fecha")
    def serializar_fecha_ecuador(self, value: datetime, _info):
        return to_ecuador(value) if value is not None else None

    class Config:
        from_attributes = True
