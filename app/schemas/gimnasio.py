from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .pago import PagoOut


class MembresiaGimnasioCreate(BaseModel):
    pacienteid: int
    fechainicio: date
    diascontratados: int = Field(default=20, ge=1, le=60)
    precio: Optional[float] = Field(default=None, ge=0)
    observaciones: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class MovimientoGimnasioCreate(BaseModel):
    pacienteid: int
    fecha: Optional[date] = None

    # 1 = asistió a gimnasio
    # 2 = terapia reemplazó gimnasio
    tipo: int = Field(..., ge=1, le=2)

    sesionid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    observacion: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class MembresiaGimnasioOut(BaseModel):
    id: int
    pacienteid: int
    fechainicio: date
    diascontratados: int
    precio: Optional[float] = None
    modalidad: str = "MENSUAL"
    activo: bool
    observaciones: Optional[str] = None
    fechacreacion: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MovimientoGimnasioOut(BaseModel):
    id: int
    membresiaid: int
    pacienteid: int
    fecha: date
    tipo: int
    sesionid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    observacion: Optional[str] = None
    fechacreacion: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ResumenMembresiaGimnasioOut(BaseModel):
    membresia: MembresiaGimnasioOut

    fecha_fin_estimada: date

    dias_contratados: int
    dias_habiles_transcurridos: int

    dias_asistidos: int
    dias_aplazados_por_terapia: int
    dias_perdidos: int
    dias_consumidos: int
    dias_restantes: int

    puede_registrar_hoy: bool
    mensaje: str

    model_config = ConfigDict(from_attributes=True)

class PaseDiarioGimnasioCreate(BaseModel):
    pacienteid: int
    fecha: Optional[date] = None
    precio: float = Field(..., gt=0)
    observacion: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class PaseDiarioGimnasioOut(BaseModel):
    paciente: Optional[str] = None
    membresia: MembresiaGimnasioOut
    movimiento: MovimientoGimnasioOut
    pago: Optional[PagoOut] = None

    model_config = ConfigDict(from_attributes=True)

class GimnasioAsistenciaRapidaCreate(BaseModel):
    fecha: Optional[date] = None
    observacion: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class GimnasioAsistenciaRapidaOut(BaseModel):
    pacienteid: int
    paciente: str
    cedula: Optional[str] = None
    consultorioid: Optional[int] = None

    membresiaid: int
    fechainicio: date
    fecha_fin_estimada: date

    dias_contratados: int
    dias_asistidos: int
    dias_aplazados_por_terapia: int
    dias_consumidos: int
    dias_restantes: int

    ultima_asistencia: Optional[date] = None
    asistencia_hoy_registrada: bool
    puede_registrar_hoy: bool
    mensaje: str

    model_config = ConfigDict(from_attributes=True)
