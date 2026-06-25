from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .tratamiento_paciente import (
    TratamientoPacienteCreate,
    TratamientoPacienteSimpleOut,
)


class DiagnosticoBase(BaseModel):
    diagnostico: str
    fechadiagnostico: date
    activo: bool = True
    notas: Optional[str] = None


class DiagnosticoCreate(DiagnosticoBase):
    pacienteid: int
    tratamientos: List[TratamientoPacienteCreate] = Field(
        ...,
        min_length=1,
        description="Al menos un tratamiento asociado",
    )


class DiagnosticoUpdate(BaseModel):
    pacienteid: Optional[int] = None
    diagnostico: Optional[str] = None
    fechadiagnostico: Optional[date] = None
    activo: Optional[bool] = None
    notas: Optional[str] = None


class DiagnosticoOut(DiagnosticoBase):
    id: int
    pacienteid: int
    fechacreacion: datetime
    tratamientos: List[TratamientoPacienteSimpleOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)