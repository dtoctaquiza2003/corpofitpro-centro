from datetime import date, time
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class InicioSesionCreate(BaseModel):
    pacienteid: int
    escaladolorentrada: int = Field(..., ge=0, le=10)

    # Si se manda, permite registrar una sesión en una fecha específica.
    # Si no se manda, se usa la fecha actual de Ecuador.
    fecha_atencion: Optional[date] = None

    # Para sesión normal se usa la hora actual.
    # Para sesión retroactiva deben venir hora_ingreso y hora_salida.
    hora_ingreso: Optional[time] = None
    hora_salida: Optional[time] = None

    # Solo obligatorio si se registra retroactiva finalizada.
    escaladolorsalida: Optional[int] = Field(default=None, ge=0, le=10)

    # Tratamiento asociado.
    tratamientopacienteid: Optional[int] = None

    # Si true, la sesión se guarda ya finalizada.
    retroactiva: bool = False

    model_config = ConfigDict(populate_by_name=True)


class FinalizarSesionCreate(BaseModel):
    escaladolorsalida: int = Field(..., ge=0, le=10)
    tratamientos: List[int] = Field(default_factory=list)

    model_config = ConfigDict(populate_by_name=True)


class SesionAtencionOut(BaseModel):
    id: int
    pacienteid: int
    terapeutaid: int
    paciente: Optional[str] = None

    fecha: date
    horaingreso: time
    horasalida: Optional[time] = None
    duracionminutos: Optional[int] = None

    escaladolorentrada: int
    escaladolorsalida: Optional[int] = None

    # Antiguo
    pacientepaqueteid: Optional[int] = None

    # Nuevo
    tratamientopacienteid: Optional[int] = None
    tratamiento: Optional[str] = None
    precio_sesion_aplicado: Optional[float] = None

    estado: str
    tratamientos: List[str] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class SesionCreate(BaseModel):
    pacienteid: int
    fecha: date
    horaingreso: time
    horasalida: time
    escaladolorentrada: int = Field(..., ge=0, le=10)
    escaladolorsalida: int = Field(..., ge=0, le=10)
    tratamientos: Optional[List[int]] = None
    tratamientopacienteid: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)


class SesionOut(SesionAtencionOut):
    pass

class TipoTratamientoOut(BaseModel):
    id: int
    nombre: str
    activo: bool

    model_config = ConfigDict(from_attributes=True)