from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_user
from ..dependencies.db import get_db
from ..models.notificacion import Notificacion
from ..models.usuario import Usuario
from ..models.usuario_dispositivo import UsuarioDispositivo
from ..schemas.notificacion import NotificacionOut, RegistrarDispositivoIn

router = APIRouter(
    prefix="/api/notificaciones",
    tags=["notificaciones"],
)


@router.get("/", response_model=List[NotificacionOut])
def listar_mis_notificaciones(
    solo_no_leidas: bool = False,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(Notificacion).filter(
        Notificacion.usuarioid == current_user.id
    )

    if solo_no_leidas:
        query = query.filter(Notificacion.leida == False)

    return (
        query.order_by(Notificacion.fecha.desc())
        .limit(100)
        .all()
    )


@router.get("/no-leidas/count")
def contar_no_leidas(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    total = (
        db.query(Notificacion)
        .filter(
            Notificacion.usuarioid == current_user.id,
            Notificacion.leida == False,
        )
        .count()
    )

    return {"total": total}


@router.patch("/{notificacion_id}/leer", response_model=NotificacionOut)
def marcar_como_leida(
    notificacion_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    notificacion = (
        db.query(Notificacion)
        .filter(
            Notificacion.id == notificacion_id,
            Notificacion.usuarioid == current_user.id,
        )
        .first()
    )

    if not notificacion:
        raise HTTPException(
            status_code=404,
            detail="Notificación no encontrada.",
        )

    notificacion.leida = True

    db.commit()
    db.refresh(notificacion)

    return notificacion


@router.patch("/leer-todas")
def marcar_todas_como_leidas(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    total = (
        db.query(Notificacion)
        .filter(
            Notificacion.usuarioid == current_user.id,
            Notificacion.leida == False,
        )
        .update(
            {Notificacion.leida: True},
            synchronize_session=False,
        )
    )

    db.commit()

    return {
        "ok": True,
        "total_actualizadas": total,
    }


@router.post("/dispositivo")
def registrar_dispositivo(
    data: RegistrarDispositivoIn,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    token = data.fcm_token.strip()

    if not token:
        raise HTTPException(
            status_code=400,
            detail="Token FCM requerido.",
        )

    dispositivo = (
        db.query(UsuarioDispositivo)
        .filter(UsuarioDispositivo.fcm_token == token)
        .first()
    )

    if dispositivo:
        dispositivo.usuarioid = current_user.id
        dispositivo.plataforma = data.plataforma
        dispositivo.activo = True
    else:
        dispositivo = UsuarioDispositivo(
            usuarioid=current_user.id,
            fcm_token=token,
            plataforma=data.plataforma,
            activo=True,
        )
        db.add(dispositivo)

    db.commit()

    return {
        "ok": True,
        "message": "Dispositivo registrado correctamente.",
    }