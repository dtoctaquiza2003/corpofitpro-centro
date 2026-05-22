from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_user
from ..auth.permissions import validar_consultorio_secretario
from ..dependencies.db import get_db
from ..models.usuario import Usuario
from ..models.usuario_permiso_temporal import UsuarioPermisoTemporal
from ..services.notificacion_service import crear_notificacion_usuario
from ..schemas.permiso_temporal import (
    PermisoTemporalCreate,
    PermisoTemporalEstadoOut,
    PermisoTemporalOut,
    TIPO_ADMIN_TEMPORAL,
    TIPO_REGISTRO_RETROACTIVO,
)

router = APIRouter(
    prefix="/api/permisos-temporales",
    tags=["permisos-temporales"],
)

TIPOS_PERMITIDOS = {
    TIPO_REGISTRO_RETROACTIVO,
    TIPO_ADMIN_TEMPORAL,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_ecuador() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))



def _nombre_usuario(usuario: Usuario) -> str:
    nombres = (usuario.nombres or "").strip()
    apellidos = (usuario.apellidos or "").strip()
    nombre = f"{nombres} {apellidos}".strip()
    return nombre or f"Usuario {usuario.id}"


def _titulo_permiso(tipo_permiso: str) -> str:
    if tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        return "Permiso retroactivo activado"

    if tipo_permiso == TIPO_ADMIN_TEMPORAL:
        return "Permiso administrativo activado"

    return "Permiso temporal activado"


def _mensaje_permiso_otorgado(
    tipo_permiso: str,
    fecha_fin: datetime,
    dias_atras_permitidos: int,
) -> str:
    vence = fecha_fin.astimezone(timezone(timedelta(hours=-5))).strftime("%d/%m/%Y %H:%M")

    if tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        if dias_atras_permitidos <= 0:
            rango = "de hoy"
        else:
            rango = "desde el lunes de esta semana"

        return (
            "Se te otorgó permiso para registrar atenciones retroactivas "
            f"{rango}. Válido hasta {vence}."
        )

    if tipo_permiso == TIPO_ADMIN_TEMPORAL:
        return (
            "Se te otorgó permiso administrativo temporal para tu consultorio. "
            f"Válido hasta {vence}."
        )

    return f"Se te otorgó un permiso temporal. Válido hasta {vence}."


def _notificar_permiso_otorgado(
    db: Session,
    permiso: UsuarioPermisoTemporal,
    usuario_objetivo: Usuario,
    current_user: Usuario,
) -> None:
    titulo = _titulo_permiso(permiso.tipo_permiso)
    mensaje = _mensaje_permiso_otorgado(
        tipo_permiso=permiso.tipo_permiso,
        fecha_fin=permiso.fecha_fin,
        dias_atras_permitidos=permiso.dias_atras_permitidos,
    )

    crear_notificacion_usuario(
        db=db,
        usuarioid=usuario_objetivo.id,
        titulo=titulo,
        mensaje=mensaje,
        tipo="permiso_temporal_otorgado",
        referencia_tipo="permiso_temporal",
        referencia_id=permiso.id,
        data={
            "tipo_permiso": permiso.tipo_permiso,
            "permiso_id": permiso.id,
            "usuario_id": usuario_objetivo.id,
            "autorizado_por_id": current_user.id,
            "autorizado_por": _nombre_usuario(current_user),
            "fecha_inicio": permiso.fecha_inicio.isoformat(),
            "fecha_fin": permiso.fecha_fin.isoformat(),
            "dias_atras_permitidos": permiso.dias_atras_permitidos,
        },
        hacer_flush=True,
        enviar_push=True,
    )


def _obtener_usuario_activo(
    db: Session,
    usuarioid: int,
) -> Usuario:
    usuario = (
        db.query(Usuario)
        .filter(
            Usuario.id == usuarioid,
            Usuario.activo == True,
        )
        .first()
    )

    if not usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado o inactivo.",
        )

    return usuario


def _validar_autorizador(
    current_user: Usuario,
    usuario_objetivo: Usuario,
    tipo_permiso: str,
) -> None:
    """
    Reglas:
    - Jefe puede activar ambos permisos.
    - Secretario puede activar solo registro retroactivo y solo para usuarios de su consultorio.
    - Administrador temporal NO puede crear permisos temporales.
    """

    if current_user.rol == 3:
        return

    if current_user.rol == 1:
        validar_consultorio_secretario(current_user, current_user.consultorioid)

        if tipo_permiso != TIPO_REGISTRO_RETROACTIVO:
            raise HTTPException(
                status_code=403,
                detail=(
                    "El secretario solo puede activar permisos de registro "
                    "retroactivo de sesiones."
                ),
            )

        if usuario_objetivo.consultorioid != current_user.consultorioid:
            raise HTTPException(
                status_code=403,
                detail="Solo puedes autorizar usuarios de tu consultorio.",
            )

        return

    raise HTTPException(
        status_code=403,
        detail="No autorizado para gestionar permisos temporales.",
    )


def obtener_permiso_temporal_activo(
    db: Session,
    usuarioid: int,
    tipo_permiso: str,
) -> Optional[UsuarioPermisoTemporal]:
    ahora = now_utc()

    return (
        db.query(UsuarioPermisoTemporal)
        .filter(
            UsuarioPermisoTemporal.usuarioid == usuarioid,
            UsuarioPermisoTemporal.tipo_permiso == tipo_permiso,
            UsuarioPermisoTemporal.activo == True,
            UsuarioPermisoTemporal.fecha_inicio <= ahora,
            UsuarioPermisoTemporal.fecha_fin >= ahora,
        )
        .order_by(UsuarioPermisoTemporal.fecha_fin.desc())
        .first()
    )


def usuario_tiene_permiso_temporal(
    db: Session,
    usuario: Usuario,
    tipo_permiso: str,
) -> bool:
    return (
        obtener_permiso_temporal_activo(
            db=db,
            usuarioid=usuario.id,
            tipo_permiso=tipo_permiso,
        )
        is not None
    )


@router.get("/", response_model=List[PermisoTemporalOut])
def listar_permisos_temporales(
    activos: bool = Query(default=True),
    tipo_permiso: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado para listar permisos temporales.",
        )

    query = db.query(UsuarioPermisoTemporal)

    if activos:
        ahora = now_utc()
        query = query.filter(
            UsuarioPermisoTemporal.activo == True,
            UsuarioPermisoTemporal.fecha_fin >= ahora,
        )

    if tipo_permiso:
        query = query.filter(UsuarioPermisoTemporal.tipo_permiso == tipo_permiso)

    if current_user.rol == 1:
        validar_consultorio_secretario(current_user, current_user.consultorioid)
        query = query.filter(
            UsuarioPermisoTemporal.consultorioid == current_user.consultorioid
        )

    return (
        query.order_by(
            UsuarioPermisoTemporal.fecha_fin.desc(),
            UsuarioPermisoTemporal.id.desc(),
        )
        .limit(100)
        .all()
    )


@router.get("/me", response_model=List[PermisoTemporalOut])
def listar_mis_permisos_temporales(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    ahora = now_utc()

    return (
        db.query(UsuarioPermisoTemporal)
        .filter(
            UsuarioPermisoTemporal.usuarioid == current_user.id,
            UsuarioPermisoTemporal.activo == True,
            UsuarioPermisoTemporal.fecha_inicio <= ahora,
            UsuarioPermisoTemporal.fecha_fin >= ahora,
        )
        .order_by(UsuarioPermisoTemporal.fecha_fin.desc())
        .all()
    )


@router.get("/me/{tipo_permiso}", response_model=PermisoTemporalEstadoOut)
def verificar_mi_permiso_temporal(
    tipo_permiso: str,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    if tipo_permiso not in TIPOS_PERMITIDOS:
        raise HTTPException(
            status_code=400,
            detail="Tipo de permiso no válido.",
        )

    permiso = obtener_permiso_temporal_activo(
        db=db,
        usuarioid=current_user.id,
        tipo_permiso=tipo_permiso,
    )

    return PermisoTemporalEstadoOut(
        activo=permiso is not None,
        tipo_permiso=tipo_permiso,
        permiso=permiso,
    )


@router.post(
    "/",
    response_model=PermisoTemporalOut,
    status_code=status.HTTP_201_CREATED,
)
def crear_permiso_temporal(
    data: PermisoTemporalCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    if data.tipo_permiso not in TIPOS_PERMITIDOS:
        raise HTTPException(
            status_code=400,
            detail="Tipo de permiso no válido.",
        )

    usuario_objetivo = _obtener_usuario_activo(db, data.usuarioid)

    if usuario_objetivo.rol != 2:
        raise HTTPException(
            status_code=400,
            detail="Solo se pueden asignar permisos temporales a terapeutas.",
        )

    if usuario_objetivo.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta no tiene consultorio asignado.",
        )

    _validar_autorizador(
        current_user=current_user,
        usuario_objetivo=usuario_objetivo,
        tipo_permiso=data.tipo_permiso,
    )

    fecha_inicio = data.fecha_inicio or now_utc()

    if data.fecha_fin <= fecha_inicio:
        raise HTTPException(
            status_code=400,
            detail="La fecha fin debe ser mayor a la fecha inicio.",
        )

    dias_atras_permitidos = data.dias_atras_permitidos

    if data.tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        hoy_ecuador = now_ecuador().date()

        # Monday = 0, Tuesday = 1, ..., Sunday = 6
        dias_desde_lunes = hoy_ecuador.weekday()

        dias_atras_permitidos = dias_desde_lunes

    if data.tipo_permiso == TIPO_ADMIN_TEMPORAL:
        if current_user.rol != 3:
            raise HTTPException(
                status_code=403,
                detail="Solo el jefe puede activar administrador temporal.",
            )

        if data.dias_atras_permitidos != 0:
            raise HTTPException(
                status_code=400,
                detail="El permiso de administrador temporal no usa días atrás.",
            )

    # Desactiva permisos anteriores del mismo tipo para evitar duplicados activos.
    db.query(UsuarioPermisoTemporal).filter(
        UsuarioPermisoTemporal.usuarioid == usuario_objetivo.id,
        UsuarioPermisoTemporal.tipo_permiso == data.tipo_permiso,
        UsuarioPermisoTemporal.activo == True,
    ).update(
        {UsuarioPermisoTemporal.activo: False},
        synchronize_session=False,
    )

    permiso = UsuarioPermisoTemporal(
        usuarioid=usuario_objetivo.id,
        autorizado_por_id=current_user.id,
        consultorioid=usuario_objetivo.consultorioid,
        tipo_permiso=data.tipo_permiso,
        fecha_inicio=fecha_inicio,
        fecha_fin=data.fecha_fin,
        dias_atras_permitidos=dias_atras_permitidos,
        motivo=data.motivo,
        activo=True,
    )

    db.add(permiso)
    db.flush()

    _notificar_permiso_otorgado(
        db=db,
        permiso=permiso,
        usuario_objetivo=usuario_objetivo,
        current_user=current_user,
    )

    db.commit()
    db.refresh(permiso)

    return permiso


@router.patch("/{permiso_id}/desactivar", response_model=PermisoTemporalOut)
def desactivar_permiso_temporal(
    permiso_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    permiso = (
        db.query(UsuarioPermisoTemporal)
        .filter(UsuarioPermisoTemporal.id == permiso_id)
        .first()
    )

    if not permiso:
        raise HTTPException(
            status_code=404,
            detail="Permiso temporal no encontrado.",
        )

    usuario_objetivo = _obtener_usuario_activo(db, permiso.usuarioid)

    _validar_autorizador(
        current_user=current_user,
        usuario_objetivo=usuario_objetivo,
        tipo_permiso=permiso.tipo_permiso,
    )

    permiso.activo = False

    db.commit()
    db.refresh(permiso)

    return permiso