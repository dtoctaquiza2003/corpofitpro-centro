from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, field_serializer

from app.utils.fechas import to_ecuador


class NotificacionOut(BaseModel):
    id: int
    usuarioid: int

    titulo: str
    mensaje: str
    tipo: str

    referencia_tipo: Optional[str] = None
    referencia_id: Optional[int] = None

    leida: bool
    fecha: datetime

    data: Optional[dict[str, Any]] = None

    @field_serializer("fecha")
    def serializar_fecha_ecuador(self, value: datetime, _info):
        return to_ecuador(value) if value is not None else None

    class Config:
        from_attributes = True


class RegistrarDispositivoIn(BaseModel):
    fcm_token: str
    plataforma: Optional[str] = None