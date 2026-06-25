from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime
from ..dependencies.db import get_db
from ..models.asistencia import Asistencia
from ..models.usuario import Usuario
from ..auth.dependencies import get_current_terapeuta
from ..schemas.asistencia import AsistenciaCreate

router = APIRouter(prefix="/api/asistencias", tags=["asistencias"])

@router.post("/", status_code=status.HTTP_201_CREATED)
def registrar_asistencia(
    data: AsistenciaCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_terapeuta)
):
    existe = db.query(Asistencia).filter(
        Asistencia.pacienteid == data.pacienteid,
        Asistencia.fecha == data.fecha
    ).first()
    if existe:
        raise HTTPException(status_code=400, detail="Ya se registró asistencia para este paciente en esta fecha")
    nueva = Asistencia(
        pacienteid=data.pacienteid,
        fecha=data.fecha,
        asistio=data.asistio,
        horaregistro=datetime.now()
    )
    db.add(nueva)
    db.commit()
    return {"ok": True}