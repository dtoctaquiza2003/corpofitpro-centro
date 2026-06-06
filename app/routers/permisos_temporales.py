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
    TIPO_CREAR_TRATAMIENTOS,
    TIPO_REGISTRO_RETROACTIVO,
    TIPO_ATENCION_SUCURSAL_TEMPORAL,
)

router = APIRouter(
    prefix="/api/permisos-temporales",
    tags=["permisos-temporales"],
)

TIPOS_PERMITIDOS = {
    TIPO_REGISTRO_RETROACTIVO,
    TIPO_ADMIN_TEMPORAL,
    TIPO_CREAR_TRATAMIENTOS,
    TIPO_ATENCION_SUCURSAL_TEMPORAL,
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def now_ecuador() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))


def _normalizar_fecha_programada(fecha: datetime) -> datetime:
    if fecha.tzinfo is None:
        fecha = fecha.replace(tzinfo=timezone(timedelta(hours=-5)))
    return fecha.astimezone(timezone.utc)


def _normalizar_rango_permiso(data: PermisoTemporalCreate) -> tuple[datetime, datetime]:
    """
    Usa la hora del servidor como fuente de verdad.

    El problema detectado fue que algunos celulares enviaban fecha_inicio/fecha_fin
    atrasadas. El permiso se creaba con activo=true, pero vencido, por eso no
    aparecía en la lista ni servía para crear tratamientos/registrar retroactivos.

    Reglas:
    - Si la app envía duracion_horas, se respeta esa duración desde la hora del servidor.
    - Si una app anterior envía fecha_inicio/fecha_fin, se calcula la duración entre ambas
      y se aplica desde la hora del servidor.
    - Si no hay datos válidos, se usa 8 horas por defecto.
    """
    if data.tipo_permiso == TIPO_ATENCION_SUCURSAL_TEMPORAL:
        if data.fecha_inicio is None or data.fecha_fin is None:
            raise HTTPException(status_code=400, detail="Para atención temporal por sucursal debes enviar fecha_inicio y fecha_fin.")
        fecha_inicio = _normalizar_fecha_programada(data.fecha_inicio)
        fecha_fin = _normalizar_fecha_programada(data.fecha_fin)
        if fecha_fin <= fecha_inicio:
            raise HTTPException(status_code=400, detail="La fecha fin debe ser mayor que la fecha inicio.")
        if (fecha_fin - fecha_inicio).total_seconds() > 72 * 3600:
            raise HTTPException(status_code=400, detail="El permiso no puede durar más de 72 horas.")
        return fecha_inicio, fecha_fin

    inicio_servidor = now_utc()

    duracion_horas = getattr(data, "duracion_horas", None)

    if duracion_horas is None and data.fecha_inicio is not None and data.fecha_fin is not None:
        duracion = data.fecha_fin - data.fecha_inicio
        segundos = int(duracion.total_seconds())

        if segundos > 0:
            duracion_horas = max(1, min(72, int(round(segundos / 3600))))

    if duracion_horas is None and data.fecha_fin is not None:
        duracion = data.fecha_fin - inicio_servidor
        segundos = int(duracion.total_seconds())

        if segundos > 0:
            duracion_horas = max(1, min(72, int(round(segundos / 3600))))

    if duracion_horas is None:
        duracion_horas = 8

    fecha_fin = inicio_servidor + timedelta(hours=duracion_horas)

    return inicio_servidor, fecha_fin


def _nombre_usuario(usuario: Usuario | None) -> str:
    if not usuario:
        return "Usuario"

    nombre = f"{usuario.nombres or ''} {usuario.apellidos or ''}".strip()

    if nombre:
        return nombre

    if getattr(usuario, "email", None):
        return usuario.email

    return f"Usuario {usuario.id}"


def _tipo_permiso_legible(tipo_permiso: str) -> str:
    if tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        return "registro retroactivo de sesiones"

    if tipo_permiso == TIPO_ADMIN_TEMPORAL:
        return "administrador temporal de consultorio"

    if tipo_permiso == TIPO_CREAR_TRATAMIENTOS:
        return "creación temporal de tratamientos"

    if tipo_permiso == TIPO_ATENCION_SUCURSAL_TEMPORAL:
        return "atención temporal por sucursal"

    return tipo_permiso.replace("_", " ")


def _formatear_fecha_permiso(fecha: datetime) -> str:
    try:
        fecha_ecuador = fecha.astimezone(timezone(timedelta(hours=-5)))
    except Exception:
        fecha_ecuador = fecha

    return fecha_ecuador.strftime("%d/%m/%Y %H:%M")


def _mensaje_permiso_otorgado(
    permiso: UsuarioPermisoTemporal,
    autorizador: Usuario,
) -> tuple[str, str]:
    tipo_legible = _tipo_permiso_legible(permiso.tipo_permiso)
    autorizador_nombre = _nombre_usuario(autorizador)
    fecha_fin = _formatear_fecha_permiso(permiso.fecha_fin)

    if permiso.tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        mensaje = (
            "Se te otorgó permiso para registrar sesiones retroactivas.\n"
            "Alcance: desde el lunes de esta semana hasta hoy.\n"
            f"Válido hasta: {fecha_fin}.\n"
            f"Autorizado por: {autorizador_nombre}."
        )
    elif permiso.tipo_permiso == TIPO_CREAR_TRATAMIENTOS:
        mensaje = (
            "Se te otorgó permiso para crear tratamientos temporalmente.\n"
            "Alcance: solo pacientes a los que ya tienes acceso.\n"
            f"Válido hasta: {fecha_fin}.\n"
            f"Autorizado por: {autorizador_nombre}."
        )
    elif permiso.tipo_permiso == TIPO_ATENCION_SUCURSAL_TEMPORAL:
        fecha_inicio = _formatear_fecha_permiso(permiso.fecha_inicio)
        mensaje = (
            "Se programó tu permiso de atención por sucursal.\n"
            "Podrás atender pacientes de tu consultorio durante el rango autorizado.\n"
            f"Inicio: {fecha_inicio}.\n"
            f"Fin: {fecha_fin}.\n"
            f"Autorizado por: {autorizador_nombre}."
        )
    else:
        mensaje = (
            f"Se te otorgó permiso de {tipo_legible}.\n"
            f"Válido hasta: {fecha_fin}.\n"
            f"Autorizado por: {autorizador_nombre}."
        )

    return "Permiso temporal activado", mensaje


def _mensaje_permiso_desactivado(
    permiso: UsuarioPermisoTemporal,
    autorizador: Usuario,
) -> tuple[str, str]:
    tipo_legible = _tipo_permiso_legible(permiso.tipo_permiso)
    autorizador_nombre = _nombre_usuario(autorizador)

    if permiso.tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        mensaje = (
            "Tu permiso para registrar sesiones retroactivas fue desactivado.\n"
            "Desde este momento ya no podrás registrar atenciones retroactivas.\n"
            f"Desactivado por: {autorizador_nombre}."
        )
    elif permiso.tipo_permiso == TIPO_CREAR_TRATAMIENTOS:
        mensaje = (
            "Tu permiso para crear tratamientos fue desactivado.\n"
            "Desde este momento ya no podrás crear tratamientos temporalmente.\n"
            f"Desactivado por: {autorizador_nombre}."
        )
    else:
        mensaje = (
            f"Tu permiso de {tipo_legible} fue desactivado.\n"
            f"Desactivado por: {autorizador_nombre}."
        )

    return "Permiso temporal desactivado", mensaje


def _notificar_permiso_temporal(
    db: Session,
    permiso: UsuarioPermisoTemporal,
    usuario_objetivo: Usuario,
    current_user: Usuario,
    activado: bool,
) -> None:
    if activado:
        titulo, mensaje = _mensaje_permiso_otorgado(
            permiso=permiso,
            autorizador=current_user,
        )
        tipo_notificacion = "permiso_temporal_otorgado"
    else:
        titulo, mensaje = _mensaje_permiso_desactivado(
            permiso=permiso,
            autorizador=current_user,
        )
        tipo_notificacion = "permiso_temporal_desactivado"

    crear_notificacion_usuario(
        db=db,
        usuarioid=usuario_objetivo.id,
        titulo=titulo,
        mensaje=mensaje,
        tipo=tipo_notificacion,
        referencia_tipo="permiso_temporal",
        referencia_id=permiso.id,
        data={
            "permiso_id": permiso.id,
            "tipo_permiso": permiso.tipo_permiso,
            "permiso_activo": activado,
            "usuario_id": usuario_objetivo.id,
            "autorizado_por_id": current_user.id,
            "fecha_inicio": permiso.fecha_inicio.isoformat()
            if permiso.fecha_inicio
            else None,
            "fecha_fin": permiso.fecha_fin.isoformat()
            if permiso.fecha_fin
            else None,
            "dias_atras_permitidos": permiso.dias_atras_permitidos,
            "actualizar": [
                "permisos_temporales",
                "sesiones",
                "tratamientos",
                "dashboard",
                "notificaciones",
            ],
        },
        hacer_flush=False,
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
    - Jefe puede activar permisos retroactivos, creación de tratamientos y admin temporal.
    - Secretario puede activar registro retroactivo y creación de tratamientos solo para usuarios de su consultorio.
    - Administrador temporal NO puede crear permisos temporales.
    """

    if current_user.rol == 3:
        return

    if current_user.rol == 1:
        validar_consultorio_secretario(current_user, current_user.consultorioid)

        if tipo_permiso not in (
            TIPO_REGISTRO_RETROACTIVO,
            TIPO_CREAR_TRATAMIENTOS,
            TIPO_ATENCION_SUCURSAL_TEMPORAL,
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "El secretario solo puede activar permisos de registro retroactivo, "
                    "creación temporal de tratamientos o atención temporal por sucursal."
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

    fecha_inicio, fecha_fin = _normalizar_rango_permiso(data)

    dias_atras_permitidos = data.dias_atras_permitidos

    if data.tipo_permiso == TIPO_REGISTRO_RETROACTIVO:
        hoy_ecuador = now_ecuador().date()

        # Monday = 0, Tuesday = 1, ..., Sunday = 6.
        # El permiso retroactivo permite desde el lunes de la semana actual.
        dias_atras_permitidos = hoy_ecuador.weekday()

    if data.tipo_permiso == TIPO_CREAR_TRATAMIENTOS:
        dias_atras_permitidos = 0

    if data.tipo_permiso == TIPO_ATENCION_SUCURSAL_TEMPORAL:
        dias_atras_permitidos = 0

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
        fecha_fin=fecha_fin,
        dias_atras_permitidos=dias_atras_permitidos,
        motivo=data.motivo,
        activo=True,
    )

    db.add(permiso)
    db.flush()

    _notificar_permiso_temporal(
        db=db,
        permiso=permiso,
        usuario_objetivo=usuario_objetivo,
        current_user=current_user,
        activado=True,
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
    db.flush()

    _notificar_permiso_temporal(
        db=db,
        permiso=permiso,
        usuario_objetivo=usuario_objetivo,
        current_user=current_user,
        activado=False,
    )

    db.commit()
    db.refresh(permiso)

    return permiso
