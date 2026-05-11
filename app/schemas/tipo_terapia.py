from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class TipoTerapiaBase(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=100)
    descripcion: Optional[str] = None
    precio_sesion: float = Field(..., gt=0)
    activo: bool = True


class TipoTerapiaCreate(TipoTerapiaBase):
    pass


class TipoTerapiaUpdate(BaseModel):
    nombre: Optional[str] = Field(None, min_length=2, max_length=100)
    descripcion: Optional[str] = None
    precio_sesion: Optional[float] = Field(None, gt=0)
    activo: Optional[bool] = None


class TipoTerapiaOut(TipoTerapiaBase):
    id: int
    fechacreacion: datetime

    model_config = ConfigDict(from_attributes=True)