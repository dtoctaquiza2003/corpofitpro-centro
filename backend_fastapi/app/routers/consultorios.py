from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from typing import List
from ..dependencies.db import get_db
from ..models.consultorio import Consultorio
from ..schemas.consultorio import ConsultorioOut
from ..auth.dependencies import get_current_user

router = APIRouter(prefix="/api/consultorios", tags=["consultorios"])

@router.get("/", response_model=List[ConsultorioOut])
def listar_consultorios(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    return db.query(Consultorio).filter(Consultorio.activo == True).all()