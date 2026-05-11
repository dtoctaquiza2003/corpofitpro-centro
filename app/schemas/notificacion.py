from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


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

    class Config:
        from_attributes = True


class RegistrarDispositivoIn(BaseModel):
    fcm_token: str
    plataforma: Optional[str] = None