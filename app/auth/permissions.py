from datetime import date, datetime, timezone
from typing import Optional

from fastapi import HTTPException

from app.models.usuario_permiso_temporal import UsuarioPermisoTemporal

from ..models.paciente import Paciente
from ..models.usuario import Usuario

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.paciente_terapeuta_compartido import PacienteTerapeutaCompartido


def es_secretario(current_user: Usuario) -> bool:
    return current_user.rol == 1


def es_terapeuta(current_user: Usuario) -> bool:
    return current_user.rol == 2


def es_jefe(current_user: Usuario) -> bool:
    return current_user.rol == 3


def validar_secretario_tiene_consultorio(current_user: Usuario) -> None:
    """
    Valida que el secretario tenga consultorio asignado.
    Solo aplica para rol 1.
    """
    if es_secretario(current_user) and current_user.consultorioid is None:
        raise HTTPException(
            status_code=403,
            detail="El secretario no tiene consultorio asignado.",
        )


def validar_consultorio_secretario(
    current_user: Usuario,
    consultorioid: Optional[int],
) -> None:
    """
    Valida que el secretario solo pueda acceder a datos de su consultorio.

    - Secretario: solo su consultorio.
    - Jefe: permitido.
    - Terapeuta: no se valida aquí.
    """
    if not es_secretario(current_user):
        return

    validar_secretario_tiene_consultorio(current_user)

    if consultorioid is None:
        raise HTTPException(
            status_code=403,
            detail="El registro no tiene consultorio asignado.",
        )

    if consultorioid != current_user.consultorioid:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para acceder a información de otro consultorio.",
        )


def validar_secretario_puede_usar_consultorio(
    current_user: Usuario,
    consultorioid: Optional[int],
) -> None:
    """
    Alias más descriptivo para usar en crear/actualizar pacientes.
    """
    validar_consultorio_secretario(current_user, consultorioid)


def terapeuta_tiene_paciente_compartido_activo(
    db: Session,
    paciente: Paciente,
    current_user: Usuario,
) -> bool:
    if not es_terapeuta(current_user):
        return False

    hoy = date.today()

    existe = (
        db.query(PacienteTerapeutaCompartido.id)
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente.id,
            PacienteTerapeutaCompartido.terapeutaid == current_user.id,
            PacienteTerapeutaCompartido.activo == True,
            or_(
                PacienteTerapeutaCompartido.fecha_inicio == None,
                PacienteTerapeutaCompartido.fecha_inicio <= hoy,
            ),
            or_(
                PacienteTerapeutaCompartido.fecha_fin == None,
                PacienteTerapeutaCompartido.fecha_fin >= hoy,
            ),
        )
        .first()
    )

    return existe is not None

def validar_acceso_paciente_por_rol(
    paciente: Paciente,
    current_user: Usuario,
    db: Optional[Session] = None,
) -> None:
    """
    Valida acceso a un paciente específico según el rol.

    Rol 1 - Secretario:
        Solo puede acceder a pacientes de su consultorio.

    Rol 2 - Terapeuta:
        Solo puede acceder a pacientes asignados a él.

    Rol 3 - Jefe:
        Puede acceder a todos los pacientes.
    """

    if es_jefe(current_user):
        return

    if es_secretario(current_user):
        validar_consultorio_secretario(
            current_user=current_user,
            consultorioid=paciente.consultorioid,
        )
        return

    if es_terapeuta(current_user):
        if paciente.terapeutaasignadoid == current_user.id:
            return

        if db is not None and terapeuta_tiene_paciente_compartido_activo(
            db=db,
            paciente=paciente,
            current_user=current_user,
        ):
            return

        raise HTTPException(
            status_code=403,
            detail="No autorizado para acceder a este paciente.",
        )

    raise HTTPException(
        status_code=403,
        detail="No autorizado.",
    )


def validar_usuario_mismo_consultorio_para_secretario(
    current_user: Usuario,
    usuario_objetivo: Usuario,
) -> None:
    """
    Útil para validar que un secretario solo pueda consultar terapeutas
    de su mismo consultorio.
    """
    if not es_secretario(current_user):
        return

    validar_secretario_tiene_consultorio(current_user)

    if usuario_objetivo.consultorioid != current_user.consultorioid:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para acceder a usuarios de otro consultorio.",
        )
    

TIPO_REGISTRO_RETROACTIVO = "registro_retroactivo_sesiones"
TIPO_ADMIN_TEMPORAL = "administrador_temporal_consultorio"
TIPO_CREAR_TRATAMIENTOS = "crear_tratamientos_paciente"


def permiso_temporal_activo(
    db: Session,
    usuario: Usuario,
    tipo_permiso: str,
) -> Optional[UsuarioPermisoTemporal]:
    ahora = datetime.now(timezone.utc)

    return (
        db.query(UsuarioPermisoTemporal)
        .filter(
            UsuarioPermisoTemporal.usuarioid == usuario.id,
            UsuarioPermisoTemporal.tipo_permiso == tipo_permiso,
            UsuarioPermisoTemporal.activo == True,
            UsuarioPermisoTemporal.fecha_inicio <= ahora,
            UsuarioPermisoTemporal.fecha_fin >= ahora,
        )
        .order_by(UsuarioPermisoTemporal.fecha_fin.desc())
        .first()
    )


def tiene_permiso_temporal(
    db: Session,
    usuario: Usuario,
    tipo_permiso: str,
) -> bool:
    return permiso_temporal_activo(
        db=db,
        usuario=usuario,
        tipo_permiso=tipo_permiso,
    ) is not None


def es_admin_temporal_consultorio(
    db: Session,
    current_user: Usuario,
) -> bool:
    if not es_terapeuta(current_user):
        return False

    return tiene_permiso_temporal(
        db=db,
        usuario=current_user,
        tipo_permiso=TIPO_ADMIN_TEMPORAL,
    )


def puede_gestionar_como_secretario_o_admin_temporal(
    db: Session,
    current_user: Usuario,
    consultorioid: Optional[int],
) -> bool:
    """
    Permite:
    - Jefe: todos los consultorios.
    - Secretario: solo su consultorio.
    - Admin temporal: solo su consultorio mientras el permiso esté activo.
    """

    if es_jefe(current_user):
        return True

    if es_secretario(current_user):
        validar_consultorio_secretario(current_user, consultorioid)
        return True

    if es_admin_temporal_consultorio(db, current_user):
        if current_user.consultorioid is None:
            return False

        if consultorioid != current_user.consultorioid:
            return False

        return True

    return False


def validar_gestion_consultorio_con_permiso_temporal(
    db: Session,
    current_user: Usuario,
    consultorioid: Optional[int],
) -> None:
    if puede_gestionar_como_secretario_o_admin_temporal(
        db=db,
        current_user=current_user,
        consultorioid=consultorioid,
    ):
        return

    raise HTTPException(
        status_code=403,
        detail="No autorizado para gestionar información de este consultorio.",
    )