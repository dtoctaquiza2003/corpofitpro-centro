from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_user
from ..auth.permissions import (
    validar_acceso_paciente_por_rol,
    validar_consultorio_secretario,
)
from ..dependencies.db import get_db
from ..models.alerta import Alerta
from ..models.paciente import Paciente
from ..models.usuario import Usuario
from ..schemas.alerta import AlertaOut

router = APIRouter(prefix="/api/alertas", tags=["alertas"])


def _validar_alerta_con_acceso(
    db: Session,
    alerta_id: int,
    current_user: Usuario,
) -> Alerta:
    alerta = db.query(Alerta).filter(Alerta.id == alerta_id).first()

    if not alerta:
        raise HTTPException(
            status_code=404,
            detail="Alerta no encontrada",
        )

    paciente = (
        db.query(Paciente)
        .filter(Paciente.id == alerta.paciente_id)
        .first()
    )

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente de la alerta no encontrado",
        )

    validar_acceso_paciente_por_rol(paciente, current_user)

    return alerta


@router.get("/", response_model=List[AlertaOut])
def listar_alertas(
    solo_no_leidas: bool = False,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(Alerta).join(
        Paciente,
        Paciente.id == Alerta.paciente_id,
    )

    if current_user.rol == 2:
        query = query.filter(
            Paciente.terapeutaasignadoid == current_user.id
        )

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        query = query.filter(
            Paciente.consultorioid == current_user.consultorioid
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    if solo_no_leidas:
        query = query.filter(Alerta.leida == False)

    return query.order_by(Alerta.fecha.desc()).all()


@router.put("/{alerta_id}/leer")
def marcar_leida(
    alerta_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    alerta = _validar_alerta_con_acceso(
        db=db,
        alerta_id=alerta_id,
        current_user=current_user,
    )

    alerta.leida = True

    db.commit()

    return {"ok": True}