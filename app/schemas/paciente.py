from pydantic import BaseModel, Field, field_validator
from datetime import date, datetime
from typing import List, Optional


class PacienteBase(BaseModel):
    nombres: str
    apellidos: str
    cedula: str = Field(..., min_length=10, max_length=10)
    fechanacimiento: date
    telefono: Optional[str] = None
    direccion: Optional[str] = None
    sexo: Optional[int] = Field(None, ge=1, le=2)
    ocupacion: Optional[str] = None
    correoelectronico: Optional[str] = None
    tiposeguro: Optional[str] = None
    motivoconsulta: Optional[str] = None
    examenescomplementarios: Optional[str] = None
    consentimientofirmado: Optional[bool] = False

    # Ahora el terapeuta es obligatorio porque de él sale el consultorio.
    terapeutaasignadoid: int = Field(..., gt=0)

    # Ya no debe ser obligatorio desde el frontend.
    # Si llega desde Flutter, el backend lo debe ignorar y reemplazar
    # por el consultorio real del terapeuta.
    consultorioid: Optional[int] = None

    @field_validator('cedula')
    @classmethod
    def validar_cedula(cls, v: str) -> str:
        if len(v) != 10 or not v.isdigit():
            raise ValueError('La cédula debe tener 10 dígitos numéricos')
        return v


class PacienteCreate(PacienteBase):
    pass


class PacienteUpdate(PacienteBase):
    pass


class PacienteOut(PacienteBase):
    id: int
    historiaclinicaid: str
    fechainicio: datetime
    estadopaciente: int
    fechaalta: Optional[date] = None
    es_cedido: bool = False
    motivo_cesion: Optional[str] = None

    class Config:
        from_attributes = True


class PacientesPageOut(BaseModel):
    items: List[PacienteOut] = Field(default_factory=list)
    total: int = 0
    limit: int = 20
    offset: int = 0
    has_more: bool = False
