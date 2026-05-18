from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_secretary, get_current_user
from ..auth.permissions import (
    validar_acceso_paciente_por_rol,
    validar_consultorio_secretario,
)
from ..dependencies.db import get_db
from ..models.paciente import Paciente
from ..models.paciente_terapeuta_compartido import PacienteTerapeutaCompartido
from ..models.usuario import Usuario
from ..schemas.paciente_compartido import (
    PacienteCompartidoCreate,
    PacienteCompartidoOut,
)
from ..services.notificacion_service import crear_notificacion_usuario

router = APIRouter(
    prefix="/api/pacientes-compartidos",
    tags=["pacientes compartidos"],
)


def _nombre_usuario(usuario: Optional[Usuario]) -> str:
    if not usuario:
        return "Sin terapeuta"
    return f"{usuario.nombres} {usuario.apellidos}".strip()


def _nombre_paciente(paciente: Optional[Paciente]) -> str:
    if not paciente:
        return "Sin paciente"
    return f"{paciente.nombres} {paciente.apellidos}".strip()


def _to_out(
    item: PacienteTerapeutaCompartido,
    paciente: Optional[Paciente] = None,
    terapeuta: Optional[Usuario] = None,
) -> PacienteCompartidoOut:
    return PacienteCompartidoOut(
        id=item.id,
        pacienteid=item.pacienteid,
        terapeutaid=item.terapeutaid,
        paciente=_nombre_paciente(paciente),
        terapeuta=_nombre_usuario(terapeuta),
        tipoterapiaid=item.tipoterapiaid,
        motivo=item.motivo,
        fecha_inicio=item.fecha_inicio,
        fecha_fin=item.fecha_fin,
        activo=item.activo,
        creado_por_id=item.creado_por_id,
        fechacreacion=item.fechacreacion,
    )


def _obtener_paciente(db: Session, pacienteid: int) -> Paciente:
    paciente = db.query(Paciente).filter(Paciente.id == pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado.",
        )

    return paciente


def _obtener_terapeuta(db: Session, terapeutaid: int) -> Usuario:
    terapeuta = (
        db.query(Usuario)
        .filter(
            Usuario.id == terapeutaid,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not terapeuta:
        raise HTTPException(
            status_code=404,
            detail="Terapeuta no encontrado o inactivo.",
        )

    if terapeuta.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta no tiene consultorio asignado.",
        )

    return terapeuta


@router.post("/", response_model=PacienteCompartidoOut, status_code=201)
def compartir_paciente(
    data: PacienteCompartidoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    paciente = _obtener_paciente(db, data.pacienteid)
    terapeuta = _obtener_terapeuta(db, data.terapeutaid)

    # Secretario solo puede compartir pacientes de su consultorio
    # y con terapeutas de su mismo consultorio.
    if current_user.rol == 1:
        validar_consultorio_secretario(current_user, paciente.consultorioid)
        validar_consultorio_secretario(current_user, terapeuta.consultorioid)

    # Jefe puede compartir entre consultorios si lo necesita.
    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado para compartir pacientes.",
        )

    if paciente.terapeutaasignadoid == terapeuta.id:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta seleccionado ya es el terapeuta principal del paciente.",
        )

    existente = (
        db.query(PacienteTerapeutaCompartido)
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente.id,
            PacienteTerapeutaCompartido.terapeutaid == terapeuta.id,
            PacienteTerapeutaCompartido.activo == True,
        )
        .first()
    )

    if existente:
        raise HTTPException(
            status_code=400,
            detail="Este paciente ya está compartido activamente con ese terapeuta.",
        )

    fecha_inicio = data.fecha_inicio or date.today()

    nuevo = PacienteTerapeutaCompartido(
        pacienteid=paciente.id,
        terapeutaid=terapeuta.id,
        tipoterapiaid=data.tipoterapiaid,
        motivo=data.motivo,
        fecha_inicio=fecha_inicio,
        fecha_fin=data.fecha_fin,
        activo=True,
        creado_por_id=current_user.id,
    )

    db.add(nuevo)
    db.flush()

    crear_notificacion_usuario(
        db=db,
        usuarioid=terapeuta.id,
        titulo="Paciente compartido contigo",
        mensaje=f"Se te autorizó atender al paciente {paciente.nombres} {paciente.apellidos}.",
        tipo="paciente_compartido",
        referencia_tipo="paciente",
        referencia_id=paciente.id,
        data={
            "paciente_id": paciente.id,
            "terapeuta_id": terapeuta.id,
            "consultorioid": paciente.consultorioid,
            "compartido_id": nuevo.id,
            "motivo": data.motivo,
            "actualizar": [
                "pacientes",
                "dashboard",
                "notificaciones",
            ],
        },
    )

    db.commit()
    db.refresh(nuevo)

    return _to_out(nuevo, paciente=paciente, terapeuta=terapeuta)


@router.get("/paciente/{pacienteid}", response_model=List[PacienteCompartidoOut])
def listar_compartidos_por_paciente(
    pacienteid: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _obtener_paciente(db, pacienteid)

    validar_acceso_paciente_por_rol(paciente, current_user)

    rows = (
        db.query(PacienteTerapeutaCompartido, Usuario)
        .join(Usuario, Usuario.id == PacienteTerapeutaCompartido.terapeutaid)
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente.id,
            PacienteTerapeutaCompartido.activo == True,
        )
        .order_by(PacienteTerapeutaCompartido.fechacreacion.desc())
        .all()
    )

    return [
        _to_out(item, paciente=paciente, terapeuta=terapeuta)
        for item, terapeuta in rows
    ]


@router.get("/terapeuta/{terapeutaid}", response_model=List[PacienteCompartidoOut])
def listar_compartidos_por_terapeuta(
    terapeutaid: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    terapeuta = _obtener_terapeuta(db, terapeutaid)

    if current_user.rol == 2 and current_user.id != terapeuta.id:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para consultar pacientes compartidos de otro terapeuta.",
        )

    if current_user.rol == 1:
        validar_consultorio_secretario(current_user, terapeuta.consultorioid)

    if current_user.rol not in (1, 2, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado.",
        )

    rows = (
        db.query(PacienteTerapeutaCompartido, Paciente)
        .join(Paciente, Paciente.id == PacienteTerapeutaCompartido.pacienteid)
        .filter(
            PacienteTerapeutaCompartido.terapeutaid == terapeuta.id,
            PacienteTerapeutaCompartido.activo == True,
        )
        .order_by(Paciente.apellidos.asc(), Paciente.nombres.asc())
        .all()
    )

    return [
        _to_out(item, paciente=paciente, terapeuta=terapeuta)
        for item, paciente in rows
    ]


@router.patch("/{compartido_id}/desactivar")
def desactivar_paciente_compartido(
    compartido_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    item = (
        db.query(PacienteTerapeutaCompartido)
        .filter(
            PacienteTerapeutaCompartido.id == compartido_id,
            PacienteTerapeutaCompartido.activo == True,
        )
        .first()
    )

    if not item:
        raise HTTPException(
            status_code=404,
            detail="Autorización compartida no encontrada o ya desactivada.",
        )

    paciente = _obtener_paciente(db, item.pacienteid)
    terapeuta = _obtener_terapeuta(db, item.terapeutaid)

    if current_user.rol == 1:
        validar_consultorio_secretario(current_user, paciente.consultorioid)
        validar_consultorio_secretario(current_user, terapeuta.consultorioid)

    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado para desactivar esta autorización.",
        )

    item.activo = False

    db.commit()

    return {
        "ok": True,
        "message": "Paciente compartido desactivado correctamente.",
    }