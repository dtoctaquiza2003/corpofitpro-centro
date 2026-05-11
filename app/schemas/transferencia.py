from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional

class TransferenciaCreate(BaseModel):
    terapeuta_origen_id: int
    terapeuta_destino_id: int
    paciente_ids: List[int]
    motivo: Optional[str] = None

class TransferenciaOut(BaseModel):
    id: int
    terapeuta_origen_id: int
    terapeuta_destino_id: int
    fecha_inicio: datetime
    activo: bool
    motivo: Optional[str]
    fecha_retorno_real: Optional[datetime]

    class Config:
        from_attributes = True