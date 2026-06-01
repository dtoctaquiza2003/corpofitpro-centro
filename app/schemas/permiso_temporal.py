from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


TIPO_REGISTRO_RETROACTIVO = "registro_retroactivo_sesiones"
TIPO_ADMIN_TEMPORAL = "administrador_temporal_consultorio"
TIPO_CREAR_TRATAMIENTOS_PACIENTE = "crear_tratamientos_paciente"


class PermisoTemporalCreate(BaseModel):
    usuarioid: int
    tipo_permiso: str = Field(..., min_length=3, max_length=80)

    fecha_inicio: Optional[datetime] = None
    fecha_fin: datetime

    dias_atras_permitidos: int = Field(default=0, ge=0, le=7)
    motivo: Optional[str] = None


class PermisoTemporalOut(BaseModel):
    id: int
    usuarioid: int
    autorizado_por_id: int
    consultorioid: Optional[int] = None

    tipo_permiso: str

    fecha_inicio: datetime
    fecha_fin: datetime

    dias_atras_permitidos: int
    motivo: Optional[str] = None

    activo: bool
    fechacreacion: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class PermisoTemporalEstadoOut(BaseModel):
    activo: bool
    tipo_permiso: str
    permiso: Optional[PermisoTemporalOut] = None