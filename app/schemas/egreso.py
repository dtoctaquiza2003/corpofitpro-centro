from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class EgresoCreate(BaseModel):
    consultorioid: Optional[int] = None
    fechaegreso: date
    categoria: str = Field(default="General", min_length=2, max_length=80)
    concepto: str = Field(..., min_length=3, max_length=200)
    monto: float = Field(..., gt=0)
    metodopago: str = Field(default="Efectivo", min_length=3, max_length=50)
    observacion: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)


class EgresoUpdate(BaseModel):
    consultorioid: Optional[int] = None
    fechaegreso: Optional[date] = None
    categoria: Optional[str] = Field(default=None, min_length=2, max_length=80)
    concepto: Optional[str] = Field(default=None, min_length=3, max_length=200)
    monto: Optional[float] = Field(default=None, gt=0)
    metodopago: Optional[str] = Field(default=None, min_length=3, max_length=50)
    observacion: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)


class EgresoAnularRequest(BaseModel):
    motivo: str = Field(..., min_length=3, max_length=500)


class EgresoOut(BaseModel):
    id: int
    consultorioid: int
    consultorio: str = "Sin consultorio"
    fechaegreso: date
    categoria: str
    concepto: str
    monto: float
    metodopago: str
    observacion: Optional[str] = None
    creado_por_id: Optional[int] = None
    creado_por: str = "Sistema"
    fechacreacion: Optional[datetime] = None
    anulado: bool = False
    motivo_anulacion: Optional[str] = None
    fecha_anulacion: Optional[datetime] = None
    anulado_por_id: Optional[int] = None
    anulado_por: Optional[str] = None


class EgresosResumenOut(BaseModel):
    desde: date
    hasta: date
    total_egresos: float = 0
    cantidad: int = 0
    egresos: List[EgresoOut] = Field(default_factory=list)
