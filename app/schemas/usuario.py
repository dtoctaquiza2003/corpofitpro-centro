from pydantic import BaseModel, EmailStr, Field
from datetime import datetime
from typing import Optional

class UsuarioBase(BaseModel):
    nombres: str
    apellidos: str
    email: EmailStr
    rol: int = Field(..., ge=1, le=3)
    fotourl: Optional[str] = None          # ← antes foto_url
    consultorioid: Optional[int] = None    # ← antes consultorio_id

class UsuarioCreate(UsuarioBase):
    password: str

class UsuarioOut(UsuarioBase):
    id: int
    activo: bool
    fecharegistro: datetime                # ← antes fecha_registro

    class Config:
        from_attributes = True

class UsuarioUpdate(BaseModel):
    nombres: Optional[str] = None
    apellidos: Optional[str] = None
    email: Optional[EmailStr] = None
    rol: Optional[int] = None
    fotourl: Optional[str] = None
    consultorioid: Optional[int] = None
    password: Optional[str] = None