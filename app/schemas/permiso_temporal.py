from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


TIPO_REGISTRO_RETROACTIVO = "registro_retroactivo_sesiones"
TIPO_ADMIN_TEMPORAL = "administrador_temporal_consultorio"
TIPO_CREAR_TRATAMIENTOS = "crear_tratamientos_paciente"
TIPO_ATENCION_SUCURSAL_TEMPORAL = "atencion_sucursal_temporal"
TIPO_MODO_PISCINA = "modo_piscina" 


class PermisoTemporalCreate(BaseModel):
    usuarioid: int
    tipo_permiso: str = Field(..., min_length=3, max_length=80)

    # Se conservan fecha_inicio/fecha_fin por compatibilidad con apps anteriores,
    # pero el backend usa la hora del servidor para evitar errores si el celular
    # tiene fecha/hora incorrecta o quedó con un estado viejo.
    fecha_inicio: Optional[datetime] = None
    fecha_fin: Optional[datetime] = None
    duracion_horas: Optional[int] = Field(default=None, ge=1, le=72)

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