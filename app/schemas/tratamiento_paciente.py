from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DiagnosticoSimpleOut(BaseModel):
    id: int
    pacienteid: int
    diagnostico: str
    fechadiagnostico: date
    activo: bool
    notas: Optional[str] = None
    fechacreacion: datetime

    model_config = ConfigDict(from_attributes=True)


class TipoTerapiaSimpleOut(BaseModel):
    id: int
    nombre: str
    descripcion: Optional[str] = None
    precio_sesion: float
    activo: bool

    model_config = ConfigDict(from_attributes=True)


class TratamientoPacienteBase(BaseModel):
    tipotratamiento: Optional[str] = None
    tipoterapiaid: Optional[int] = None

    precio_sesion_aplicado: Optional[float] = Field(None, gt=0)
    sesiones_estimadas: Optional[int] = Field(None, ge=0)
    motivo_precio_especial: Optional[str] = None

    fechainicio: date
    fechafin: Optional[date] = None
    observaciones: Optional[str] = None
    activo: bool = True
    diagnosticoid: Optional[int] = None

    @field_validator("motivo_precio_especial")
    @classmethod
    def limpiar_motivo(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        texto = value.strip()

        return texto if texto else None


class TratamientoPacienteCreate(TratamientoPacienteBase):
    pacienteid: int


class TratamientoPacienteUpdate(BaseModel):
    pacienteid: Optional[int] = None
    tipotratamiento: Optional[str] = None
    tipoterapiaid: Optional[int] = None

    precio_sesion_aplicado: Optional[float] = Field(None, gt=0)
    sesiones_estimadas: Optional[int] = Field(None, ge=0)
    motivo_precio_especial: Optional[str] = None

    fechainicio: Optional[date] = None
    fechafin: Optional[date] = None
    observaciones: Optional[str] = None
    activo: Optional[bool] = None
    diagnosticoid: Optional[int] = None


class TratamientoPacienteAumentarSesiones(BaseModel):
    cantidad: int = Field(..., gt=0, le=300)
    motivo: Optional[str] = None

    @field_validator("motivo")
    @classmethod
    def limpiar_motivo_incremento(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None

        texto = value.strip()

        return texto if texto else None


class TratamientoPacienteSimpleOut(BaseModel):
    id: int
    pacienteid: int
    diagnosticoid: Optional[int] = None

    tipotratamiento: str
    tipoterapiaid: Optional[int] = None

    precio_sesion_oficial: Optional[float] = None
    precio_sesion_aplicado: Optional[float] = None
    sesiones_estimadas: Optional[int] = None
    motivo_precio_especial: Optional[str] = None

    fechainicio: date
    fechafin: Optional[date] = None
    observaciones: Optional[str] = None
    activo: bool
    fechacreacion: datetime

    model_config = ConfigDict(from_attributes=True)


class TratamientoPacienteOut(TratamientoPacienteSimpleOut):
    diagnostico: Optional[DiagnosticoSimpleOut] = None
    tipo_terapia: Optional[TipoTerapiaSimpleOut] = None