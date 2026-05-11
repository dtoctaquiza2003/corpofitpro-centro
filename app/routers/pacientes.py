from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload
from typing import List, Optional
from pydantic import BaseModel

from ..services.notificacion_service import crear_notificacion_usuario
from ..dependencies.db import get_db
from ..models.paciente import Paciente
from ..models.usuario import Usuario
from ..models.transferencia import Transferencia
from ..auth.dependencies import get_current_user, get_current_secretary
from ..schemas.paciente import PacienteCreate, PacienteOut
from ..auth.permissions import (
    validar_acceso_paciente_por_rol,
    validar_consultorio_secretario,
)

router = APIRouter(prefix="/api/pacientes", tags=["pacientes"])


class ReasignacionMasiva(BaseModel):
    terapeuta_origen_id: int
    terapeuta_destino_id: int


class ReasignacionSeleccionados(BaseModel):
    terapeuta_origen_id: int
    terapeuta_destino_id: int
    paciente_ids: List[int]
    rango_horas: Optional[int] = None


def _paciente_to_out(
    paciente: Paciente,
    es_cedido: bool = False,
    motivo_cesion: Optional[str] = None,
) -> PacienteOut:
    data = {c.name: getattr(paciente, c.name) for c in paciente.__table__.columns}
    data["es_cedido"] = es_cedido
    data["motivo_cesion"] = motivo_cesion
    return PacienteOut(**data)


def obtener_terapeuta_y_consultorio(
    db: Session,
    terapeuta_id: int,
) -> tuple[Usuario, int]:
    terapeuta = (
        db.query(Usuario)
        .filter(
            Usuario.id == terapeuta_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not terapeuta:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta asignado no existe o no está activo.",
        )

    if terapeuta.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta seleccionado no tiene consultorio asignado.",
        )

    return terapeuta, terapeuta.consultorioid


def validar_terapeutas_para_secretario(
    current_user: Usuario,
    origen: Usuario,
    destino: Usuario,
) -> None:
    """
    Si el usuario autenticado es secretario, valida que tanto el terapeuta
    origen como el terapeuta destino pertenezcan a su mismo consultorio.
    """
    if current_user.rol != 1:
        return

    validar_consultorio_secretario(current_user, origen.consultorioid)
    validar_consultorio_secretario(current_user, destino.consultorioid)


# ============================================================
# LISTAR PACIENTES
# ============================================================

@router.get("/", response_model=List[PacienteOut])
def listar_pacientes(
    terapeuta_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    # TERAPEUTA: solo ve sus pacientes asignados
    if current_user.rol == 2:
        propios = (
            db.query(Paciente)
            .filter(Paciente.terapeutaasignadoid == current_user.id)
            .order_by(Paciente.apellidos.asc(), Paciente.nombres.asc())
            .all()
        )

        transferencias = (
            db.query(Transferencia)
            .filter(
                Transferencia.terapeuta_destino_id == current_user.id,
                Transferencia.activo == True,
            )
            .options(joinedload(Transferencia.pacientes))
            .all()
        )

        cedidos_ids = set()
        motivo_map = {}

        for transferencia in transferencias:
            for paciente in transferencia.pacientes:
                cedidos_ids.add(paciente.id)
                motivo_map[paciente.id] = transferencia.motivo

        resultado = []

        for paciente in propios:
            es_cedido = paciente.id in cedidos_ids
            motivo = motivo_map.get(paciente.id) if es_cedido else None
            resultado.append(_paciente_to_out(paciente, es_cedido, motivo))

        return resultado

    # SECRETARIO: solo ve pacientes de su consultorio
    if current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        query = db.query(Paciente).filter(
            Paciente.consultorioid == current_user.consultorioid
        )

        if terapeuta_id:
            query = query.filter(Paciente.terapeutaasignadoid == terapeuta_id)

        pacientes = query.order_by(
            Paciente.apellidos.asc(),
            Paciente.nombres.asc(),
        ).all()

        return [_paciente_to_out(paciente) for paciente in pacientes]

    # JEFE: ve todos los pacientes
    if current_user.rol == 3:
        query = db.query(Paciente)

        if terapeuta_id:
            query = query.filter(Paciente.terapeutaasignadoid == terapeuta_id)

        pacientes = query.order_by(
            Paciente.apellidos.asc(),
            Paciente.nombres.asc(),
        ).all()

        return [_paciente_to_out(paciente) for paciente in pacientes]

    raise HTTPException(
        status_code=403,
        detail="No autorizado para listar pacientes.",
    )


@router.get("/terapeuta/{terapeuta_id}", response_model=List[PacienteOut])
def listar_pacientes_por_terapeuta(
    terapeuta_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    terapeuta = (
        db.query(Usuario)
        .filter(
            Usuario.id == terapeuta_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not terapeuta:
        raise HTTPException(
            status_code=404,
            detail="Terapeuta no encontrado",
        )

    # TERAPEUTA: solo puede consultar sus propios pacientes
    if current_user.rol == 2 and current_user.id != terapeuta_id:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    # SECRETARIO: solo puede consultar terapeutas de su consultorio
    if current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            terapeuta.consultorioid,
        )

    if current_user.rol not in (1, 2, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    pacientes = (
        db.query(Paciente)
        .filter(Paciente.terapeutaasignadoid == terapeuta_id)
        .order_by(Paciente.apellidos.asc(), Paciente.nombres.asc())
        .all()
    )

    return [_paciente_to_out(paciente) for paciente in pacientes]


# ============================================================
# TRANSFERENCIAS / REASIGNACIONES
# IMPORTANTE: estas rutas van antes de /{paciente_id}
# ============================================================

@router.put("/transferir")
def transferir_pacientes(
    data: ReasignacionMasiva,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    if data.terapeuta_origen_id == data.terapeuta_destino_id:
        raise HTTPException(
            status_code=400,
            detail="No se puede transferir al mismo terapeuta.",
        )

    origen = (
        db.query(Usuario)
        .filter(
            Usuario.id == data.terapeuta_origen_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    destino = (
        db.query(Usuario)
        .filter(
            Usuario.id == data.terapeuta_destino_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not origen:
        raise HTTPException(
            status_code=404,
            detail="El terapeuta origen no existe o no está activo.",
        )

    if not destino:
        raise HTTPException(
            status_code=404,
            detail="El terapeuta destino no existe o no está activo.",
        )

    if origen.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta origen no tiene consultorio asignado.",
        )

    if destino.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta destino no tiene consultorio asignado.",
        )

    # SECRETARIO: no puede transferir pacientes de otra clínica
    validar_terapeutas_para_secretario(current_user, origen, destino)

    pacientes = (
        db.query(Paciente)
        .filter(Paciente.terapeutaasignadoid == origen.id)
        .all()
    )

    if not pacientes:
        return {
            "message": "No hay pacientes para transferir.",
            "total_transferidos": 0,
            "terapeuta_origen_id": origen.id,
            "terapeuta_destino_id": destino.id,
            "consultorio_destino_id": destino.consultorioid,
        }

    paciente_ids = []
    paciente_nombres = []

    for paciente in pacientes:
        paciente.terapeutaasignadoid = destino.id
        paciente.consultorioid = destino.consultorioid

        paciente_ids.append(paciente.id)
        paciente_nombres.append(f"{paciente.nombres} {paciente.apellidos}")

    crear_notificacion_usuario(
        db=db,
        usuarioid=destino.id,
        titulo="Pacientes reasignados",
        mensaje=f"Se te reasignaron {len(pacientes)} pacientes.",
        tipo="paciente_reasignado",
        referencia_tipo="reasignacion",
        referencia_id=None,
        data={
            "paciente_ids": paciente_ids,
            "pacientes": paciente_nombres,
            "terapeuta_anterior_id": origen.id,
            "terapeuta_nuevo_id": destino.id,
            "consultorioid": destino.consultorioid,
            "actualizado_por_id": current_user.id,
            "actualizar": [
                "pacientes",
                "sesiones",
                "dashboard",
                "notificaciones",
            ],
        },
    )

    db.commit()

    return {
        "message": "Pacientes transferidos correctamente.",
        "total_transferidos": len(pacientes),
        "terapeuta_origen_id": origen.id,
        "terapeuta_destino_id": destino.id,
        "consultorio_destino_id": destino.consultorioid,
    }

@router.put("/transferir/seleccionados")
def transferir_pacientes_seleccionados(
    data: ReasignacionSeleccionados,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    if data.terapeuta_origen_id == data.terapeuta_destino_id:
        raise HTTPException(
            status_code=400,
            detail="No se puede transferir pacientes al mismo terapeuta.",
        )

    if not data.paciente_ids:
        raise HTTPException(
            status_code=400,
            detail="Debe seleccionar al menos un paciente.",
        )

    origen = (
        db.query(Usuario)
        .filter(
            Usuario.id == data.terapeuta_origen_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    destino = (
        db.query(Usuario)
        .filter(
            Usuario.id == data.terapeuta_destino_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not origen:
        raise HTTPException(
            status_code=404,
            detail="El terapeuta origen no existe o no está activo.",
        )

    if not destino:
        raise HTTPException(
            status_code=404,
            detail="El terapeuta destino no existe o no está activo.",
        )

    if origen.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta origen no tiene consultorio asignado.",
        )

    if destino.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta destino no tiene consultorio asignado.",
        )

    # SECRETARIO: origen y destino deben ser de su mismo consultorio
    validar_terapeutas_para_secretario(current_user, origen, destino)

    pacientes = (
        db.query(Paciente)
        .filter(Paciente.id.in_(data.paciente_ids))
        .all()
    )

    if not pacientes:
        raise HTTPException(
            status_code=404,
            detail="No se encontraron pacientes para transferir.",
        )

    pacientes_no_pertenecen = []
    pacientes_fuera_consultorio = []

    for paciente in pacientes:
        if paciente.terapeutaasignadoid != data.terapeuta_origen_id:
            pacientes_no_pertenecen.append(
                {
                    "paciente_id": paciente.id,
                    "terapeuta_actual_id": paciente.terapeutaasignadoid,
                }
            )

        if current_user.rol == 1 and paciente.consultorioid != current_user.consultorioid:
            pacientes_fuera_consultorio.append(
                {
                    "paciente_id": paciente.id,
                    "consultorioid": paciente.consultorioid,
                }
            )

    if pacientes_no_pertenecen:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Algunos pacientes no pertenecen al terapeuta origen seleccionado.",
                "pacientes": pacientes_no_pertenecen,
            },
        )

    if pacientes_fuera_consultorio:
        raise HTTPException(
            status_code=403,
            detail={
                "message": "No autorizado para transferir pacientes de otro consultorio.",
                "pacientes": pacientes_fuera_consultorio,
            },
        )

    paciente_ids = []
    paciente_nombres = []

    for paciente in pacientes:
        paciente.terapeutaasignadoid = destino.id
        paciente.consultorioid = destino.consultorioid

        paciente_ids.append(paciente.id)
        paciente_nombres.append(f"{paciente.nombres} {paciente.apellidos}")

    if len(pacientes) == 1:
        titulo = "Paciente reasignado"
        mensaje = f"Se te reasignó el paciente {paciente_nombres[0]}."
        referencia_tipo = "paciente"
        referencia_id = paciente_ids[0]
    else:
        titulo = "Pacientes reasignados"
        mensaje = f"Se te reasignaron {len(pacientes)} pacientes."
        referencia_tipo = "reasignacion"
        referencia_id = None

    crear_notificacion_usuario(
        db=db,
        usuarioid=destino.id,
        titulo=titulo,
        mensaje=mensaje,
        tipo="paciente_reasignado",
        referencia_tipo=referencia_tipo,
        referencia_id=referencia_id,
        data={
            "paciente_ids": paciente_ids,
            "pacientes": paciente_nombres,
            "terapeuta_anterior_id": origen.id,
            "terapeuta_nuevo_id": destino.id,
            "consultorioid": destino.consultorioid,
            "actualizado_por_id": current_user.id,
            "actualizar": [
                "pacientes",
                "sesiones",
                "dashboard",
                "notificaciones",
            ],
        },
    )

    db.commit()

    return {
        "message": "Pacientes reasignados correctamente.",
        "total_transferidos": len(pacientes),
        "terapeuta_origen_id": origen.id,
        "terapeuta_destino_id": destino.id,
        "consultorio_destino_id": destino.consultorioid,
        "paciente_ids": data.paciente_ids,
    }


# ============================================================
# CREAR PACIENTE
# ============================================================

@router.post("/", response_model=PacienteOut, status_code=status.HTTP_201_CREATED)
def crear_paciente(
    paciente: PacienteCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    existing = db.query(Paciente).filter(Paciente.cedula == paciente.cedula).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Ya existe un paciente con esa cédula.",
                "paciente_id": existing.id,
            },
        )

    terapeuta, consultorio_id = obtener_terapeuta_y_consultorio(
        db,
        paciente.terapeutaasignadoid,
    )

    # SECRETARIO: solo puede crear pacientes para terapeutas de su consultorio
    validar_consultorio_secretario(current_user, consultorio_id)

    data = paciente.model_dump()

    data.pop("historiaclinicaid", None)
    data.pop("fechainicio", None)
    data.pop("estadopaciente", None)
    data.pop("fechaalta", None)

    data["consultorioid"] = consultorio_id
    data["terapeutaasignadoid"] = terapeuta.id

    nuevo = Paciente(**data)

    db.add(nuevo)

    # Necesitamos flush para obtener nuevo.id antes del commit.
    db.flush()

    crear_notificacion_usuario(
        db=db,
        usuarioid=terapeuta.id,
        titulo="Nuevo paciente asignado",
        mensaje=f"Se te asignó el paciente {nuevo.nombres} {nuevo.apellidos}.",
        tipo="paciente_asignado",
        referencia_tipo="paciente",
        referencia_id=nuevo.id,
        data={
            "paciente_id": nuevo.id,
            "consultorioid": nuevo.consultorioid,
            "terapeuta_id": terapeuta.id,
            "creado_por_id": current_user.id,
            "actualizar": [
                "pacientes",
                "dashboard",
                "notificaciones",
            ],
        },
    )

    db.commit()
    db.refresh(nuevo)

    return _paciente_to_out(nuevo)


# ============================================================
# RUTAS CON /{paciente_id}
# IMPORTANTE: van después de /transferir y /transferir/seleccionados
# ============================================================

@router.get("/{paciente_id}", response_model=PacienteOut)
def obtener_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = db.query(Paciente).filter(Paciente.id == paciente_id).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    validar_acceso_paciente_por_rol(paciente, current_user)

    return _paciente_to_out(paciente)


@router.put("/{paciente_id}", response_model=PacienteOut)
def actualizar_paciente(
    paciente_id: int,
    paciente: PacienteCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    db_paciente = db.query(Paciente).filter(Paciente.id == paciente_id).first()

    if not db_paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    terapeuta_anterior_id = db_paciente.terapeutaasignadoid

    # SECRETARIO: solo puede actualizar pacientes de su consultorio
    validar_acceso_paciente_por_rol(db_paciente, current_user)

    paciente_con_misma_cedula = (
        db.query(Paciente)
        .filter(
            Paciente.cedula == paciente.cedula,
            Paciente.id != paciente_id,
        )
        .first()
    )

    if paciente_con_misma_cedula:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Ya existe otro paciente registrado con esa cédula.",
                "paciente_id": paciente_con_misma_cedula.id,
            },
        )

    terapeuta, consultorio_id = obtener_terapeuta_y_consultorio(
        db,
        paciente.terapeutaasignadoid,
    )

    # SECRETARIO: no puede mover el paciente a un terapeuta de otra clínica
    validar_consultorio_secretario(current_user, consultorio_id)

    data = paciente.model_dump()

    data.pop("historiaclinicaid", None)
    data.pop("fechainicio", None)
    data.pop("estadopaciente", None)
    data.pop("fechaalta", None)

    data["consultorioid"] = consultorio_id
    data["terapeutaasignadoid"] = terapeuta.id

    for key, value in data.items():
        setattr(db_paciente, key, value)

    if (
        terapeuta_anterior_id is not None
        and terapeuta_anterior_id != db_paciente.terapeutaasignadoid
    ):
        crear_notificacion_usuario(
            db=db,
            usuarioid=db_paciente.terapeutaasignadoid,
            titulo="Paciente reasignado",
            mensaje=(
                f"Se te reasignó el paciente "
                f"{db_paciente.nombres} {db_paciente.apellidos}."
            ),
            tipo="paciente_reasignado",
            referencia_tipo="paciente",
            referencia_id=db_paciente.id,
            data={
                "paciente_id": db_paciente.id,
                "consultorioid": db_paciente.consultorioid,
                "terapeuta_anterior_id": terapeuta_anterior_id,
                "terapeuta_nuevo_id": db_paciente.terapeutaasignadoid,
                "actualizado_por_id": current_user.id,
                "actualizar": [
                    "pacientes",
                    "sesiones",
                    "dashboard",
                    "notificaciones",
                ],
            },
        )

    db.commit()
    db.refresh(db_paciente)

    return _paciente_to_out(db_paciente)

@router.delete("/{paciente_id}")
def eliminar_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    paciente = db.query(Paciente).filter(Paciente.id == paciente_id).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    # SECRETARIO: solo puede eliminar pacientes de su consultorio
    validar_acceso_paciente_por_rol(paciente, current_user)

    db.delete(paciente)
    db.commit()

    return {"ok": True}