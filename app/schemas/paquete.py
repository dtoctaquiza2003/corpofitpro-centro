from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PaqueteBase(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=100)
    cantidad_sesiones: int = Field(0, ge=0)
    precio_oficial: float = Field(..., gt=0)
    duracion_dias: Optional[int] = Field(None, gt=0)
    activo: bool = True

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    @model_validator(mode="after")
    def validar_paquete(self):
        if self.duracion_dias is None and self.cantidad_sesiones <= 0:
            raise ValueError(
                "Los paquetes sin duración deben tener al menos una sesión"
            )

        return self


class PaqueteCreate(PaqueteBase):
    pass


class PaqueteUpdate(BaseModel):
    nombre: Optional[str] = Field(None, min_length=2, max_length=100)
    cantidad_sesiones: Optional[int] = Field(None, ge=0)
    precio_oficial: Optional[float] = Field(None, gt=0)
    duracion_dias: Optional[int] = Field(None, gt=0)
    activo: Optional[bool] = None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PaqueteEstadoUpdate(BaseModel):
    activo: bool


class PaqueteOut(PaqueteBase):
    id: int


class PacientePaqueteCreate(BaseModel):
    pacienteid: int
    paqueteid: int
    preciofinal: float = Field(..., gt=0)
    sesionescontratadas: int = Field(0, ge=0)

    model_config = ConfigDict(populate_by_name=True)


class AsignarPaqueteConPagoCreate(BaseModel):
    pacienteid: int
    paqueteid: int
    preciofinal: float = Field(..., gt=0)
    sesionescontratadas: int = Field(0, ge=0)

    monto: float = Field(0, ge=0)
    metodopago: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class AsignarPaqueteConPagoOut(BaseModel):
    pacientepaqueteid: int
    pagoid: Optional[int] = None
    preciofinal: float
    monto_pagado: float
    saldo: float
    fechaexpiracion: Optional[date] = None
    message: str