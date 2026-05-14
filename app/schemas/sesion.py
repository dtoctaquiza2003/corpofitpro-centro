from datetime import date, time
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class InicioSesionCreate(BaseModel):
    pacienteid: int
    escaladolorentrada: int = Field(..., ge=0, le=10)

    # Nuevo flujo.
    # Si el paciente solo tiene un tratamiento activo, puede omitirse.
    # Si tiene varios, el backend pedirá seleccionar uno.
    tratamientopacienteid: Optional[int] = None

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