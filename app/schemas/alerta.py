from pydantic import BaseModel,Field
from datetime import datetime

class AlertaOut(BaseModel):
    id: int
    pacienteid: int = Field(..., alias="paciente_id")
    tipo: str
    descripcion: str
    fecha: datetime
    leida: bool

    class Config:
        from_attributes = True
        populate_by_name = True