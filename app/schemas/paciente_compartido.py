from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class PacienteCompartidoCreate(BaseModel):
    pacienteid: int = Field(..., gt=0)
    terapeutaid: int = Field(..., gt=0)

    tipoterapiaid: Optional[int] = None
    motivo: Optional[str] = None

    fecha_inicio: Optional[date] = None
    fecha_fin: Optional[date] = None


class PacienteCompartidoOut(BaseModel):
    id: int

    pacienteid: int
    terapeutaid: int

    paciente: Optional[str] = None
    terapeuta: Optional[str] = None

    tipoterapiaid: Optional[int] = None
    motivo: Optional[str] = None

    fecha_inicio: Optional[date] = None
    fecha_fin: Optional[date] = None

    activo: bool
    creado_por_id: Optional[int] = None
    fechacreacion: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)