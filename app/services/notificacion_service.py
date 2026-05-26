from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models.notificacion import Notificacion
from ..models.usuario_dispositivo import UsuarioDispositivo
from ..services.firebase_service import enviar_push_tokens


# Solo 2 envíos FCM en paralelo por proceso.
# Evita que una acción que cree varias notificaciones bloquee el request
# y mantenga una conexión de PostgreSQL ocupada mientras Firebase responde.
_push_executor = ThreadPoolExecutor(max_workers=2)


def _obtener_tokens_usuario(
    db: Session,
    usuarioid: int,
) -> list[str]:
    dispositivos = (
        db.query(UsuarioDispositivo)
        .filter(
            UsuarioDispositivo.usuarioid == usuarioid,
            UsuarioDispositivo.activo == True,
        )
        .all()
    )

    tokens: list[str] = []
    tokens_vistos: set[str] = set()

    for dispositivo in dispositivos:
        token = (dispositivo.fcm_token or "").strip()

        if not token or token in tokens_vistos:
            continue

        tokens.append(token)
        tokens_vistos.add(token)

    return tokens


def _enviar_push_seguro(
    *,
    tokens: list[str],
    titulo: str,
    mensaje: str,
    data: dict[str, Any],
) -> None:
    try:
        enviar_push_tokens(
            tokens=tokens,
            titulo=titulo,
            mensaje=mensaje,
            data=data,
        )
    except Exception as e:
        print(f"❌ Error enviando push FCM en segundo plano: {e}")


def _programar_envio_push(
    *,
    tokens: list[str],
    titulo: str,
    mensaje: str,
    data: dict[str, Any],
) -> None:
    if not tokens:
        print("ℹ️ Usuario sin tokens FCM activos.")
        return

    # No enviamos la sesión de SQLAlchemy al hilo.
    # Primero se copian tokens/data y luego Firebase se ejecuta aparte.
    # Así el request puede terminar y liberar la conexión a Supabase antes.
    _push_executor.submit(
        _enviar_push_seguro,
        tokens=tokens,
        titulo=titulo,
        mensaje=mensaje,
        data=data,
    )


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

    # Para el push necesitamos notificacion.id. flush no hace commit,
    # solo obtiene el ID dentro de la transacción actual.
    if hacer_flush or enviar_push:
        db.flush()

    if enviar_push:
        try:
            tokens = _obtener_tokens_usuario(
                db=db,
                usuarioid=usuarioid,
            )

            data_push = dict(data or {})
            data_push.update(
                {
                    "notificacion_id": notificacion.id,
                    "usuarioid": usuarioid,
                    "titulo": titulo,
                    "mensaje": mensaje,
                    "tipo": tipo,
                    "referencia_tipo": referencia_tipo or "",
                    "referencia_id": referencia_id or "",
                }
            )

            _programar_envio_push(
                tokens=tokens,
                titulo=titulo,
                mensaje=mensaje,
                data=data_push,
            )
        except Exception as e:
            # La creación de la notificación no debe fallar por Firebase/tokens.
            print(f"❌ Error preparando push de notificación: {e}")

    return notificacion
