from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models.notificacion import Notificacion
from ..models.usuario_dispositivo import UsuarioDispositivo
from ..services.firebase_service import enviar_push_tokens


def _enviar_push_notificacion(
    db: Session,
    notificacion: Notificacion,
) -> None:
    try:
        dispositivos = (
            db.query(UsuarioDispositivo)
            .filter(
                UsuarioDispositivo.usuarioid == notificacion.usuarioid,
                UsuarioDispositivo.activo == True,
            )
            .all()
        )

        tokens = [d.fcm_token for d in dispositivos if d.fcm_token]

        if not tokens:
            print(
                f"ℹ️ Usuario {notificacion.usuarioid} "
                f"no tiene tokens FCM activos."
            )
            return

        data_push = dict(notificacion.data or {})

        data_push.update(
            {
                "notificacion_id": notificacion.id,
                "tipo": notificacion.tipo,
                "referencia_tipo": notificacion.referencia_tipo or "",
                "referencia_id": notificacion.referencia_id or "",
            }
        )

        enviar_push_tokens(
            tokens=tokens,
            titulo=notificacion.titulo,
            mensaje=notificacion.mensaje,
            data=data_push,
        )

    except Exception as e:
        print(f"❌ Error enviando push de notificación: {e}")


def crear_notificacion_usuario(
    db: Session,
    usuarioid: int,
    titulo: str,
    mensaje: str,
    tipo: str,
    referencia_tipo: Optional[str] = None,
    referencia_id: Optional[int] = None,
    data: Optional[dict[str, Any]] = None,
    hacer_flush: bool = True,
    enviar_push: bool = True,
) -> Notificacion:
    notificacion = Notificacion(
        usuarioid=usuarioid,
        titulo=titulo,
        mensaje=mensaje,
        tipo=tipo,
        referencia_tipo=referencia_tipo,
        referencia_id=referencia_id,
        data=data,
        leida=False,
    )

    db.add(notificacion)

    # Si se va a enviar push, necesitamos el ID de la notificación.
    if hacer_flush or enviar_push:
        db.flush()

    if enviar_push:
        _enviar_push_notificacion(
            db=db,
            notificacion=notificacion,
        )

    return notificacion