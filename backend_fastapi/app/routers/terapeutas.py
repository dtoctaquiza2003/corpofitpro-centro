from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
from ..dependencies.db import get_db
from ..models.usuario import Usuario
from ..auth.dependencies import get_current_jefe
from ..schemas.usuario import UsuarioOut

router = APIRouter(prefix="/api/terapeutas", tags=["terapeutas"])

@router.get("/consultorio/{consultorio_id}", response_model=List[UsuarioOut])
def listar_terapeutas_por_consultorio(
    consultorio_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe)
):
    terapeutas = db.query(Usuario).filter(
        Usuario.rol == 2,
        Usuario.consultorioid == consultorio_id,
        Usuario.activo == True
    ).all()
    return terapeutas