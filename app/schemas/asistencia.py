from pydantic import BaseModel, Field
from datetime import date

class AsistenciaCreate(BaseModel):
    pacienteid: int = Field(..., alias="paciente_id")
    fecha: date
    asistio: bool = True

    class Config:
        populate_by_name = True