from sqlalchemy import Column, Integer, String, DateTime, Boolean, SmallInteger, ForeignKey
from sqlalchemy.sql import func
from ..database import Base

class Usuario(Base):
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nombres = Column(String(100), nullable=False)
    apellidos = Column(String(100), nullable=False)
    email = Column(String(100), unique=True, nullable=False)
    passwordhash = Column(String(255), nullable=False)  # ← Antes 'password_hash'
    rol = Column(SmallInteger, nullable=False)
    fotourl = Column(String(500))                      # ← Antes 'foto_url'
    consultorioid = Column(Integer, ForeignKey("consultorios.id"), nullable=True)  # ← Antes 'consultorio_id'
    fecharegistro = Column(DateTime(timezone=True), server_default=func.current_timestamp())  # ← Antes 'fecha_registro'
    activo = Column(Boolean, default=True)