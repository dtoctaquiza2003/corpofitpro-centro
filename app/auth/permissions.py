from typing import Optional

from fastapi import HTTPException

from ..models.paciente import Paciente
from ..models.usuario import Usuario


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


def validar_acceso_paciente_por_rol(
    paciente: Paciente,
    current_user: Usuario,
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
        if paciente.terapeutaasignadoid != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="No autorizado para acceder a este paciente.",
            )
        return

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