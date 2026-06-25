from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from ..auth.dependencies import get_current_secretary, get_current_user
from ..auth.permissions import (
    validar_acceso_paciente_por_rol,
    validar_consultorio_secretario,
)
from ..dependencies.db import get_db
from ..models.paciente import Paciente
from ..models.transferencia import Transferencia, transferencia_paciente
from ..models.usuario import Usuario
from ..schemas.transferencia import TransferenciaCreate, TransferenciaOut
from ..services.notificacion_service import crear_notificacion_usuario

router = APIRouter(prefix="/api/transferencias", tags=["transferencias"])


def _obtener_terapeuta_activo(
    db: Session,
    terapeuta_id: int,
) -> Usuario:
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

    if terapeuta.consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="El terapeuta no tiene consultorio asignado.",
        )

    return terapeuta


def _validar_terapeutas_para_secretario(
    current_user: Usuario,
    origen: Usuario,
    destino: Usuario,
) -> None:
    if current_user.rol != 1:
        return

    validar_consultorio_secretario(current_user, origen.consultorioid)
    validar_consultorio_secretario(current_user, destino.consultorioid)


def _validar_transferencia_para_secretario(
    current_user: Usuario,
    transferencia: Transferencia,
    db: Session,
) -> None:
    if current_user.rol != 1:
        return

    origen = _obtener_terapeuta_activo(
        db=db,
        terapeuta_id=transferencia.terapeuta_origen_id,
    )

    destino = _obtener_terapeuta_activo(
        db=db,
        terapeuta_id=transferencia.terapeuta_destino_id,
    )

    _validar_terapeutas_para_secretario(
        current_user=current_user,
        origen=origen,
        destino=destino,
    )

    for paciente in transferencia.pacientes:
        validar_acceso_paciente_por_rol(paciente, current_user)


@router.post("/temporales", response_model=TransferenciaOut, status_code=201)
def crear_transferencia_temporal(
    data: TransferenciaCreate,
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

    pacientes = (
        db.query(Paciente)
        .filter(Paciente.id.in_(data.paciente_ids))
        .all()
    )

    if len(pacientes) != len(data.paciente_ids):
        raise HTTPException(
            status_code=404,
            detail="Algún paciente no existe",
        )

    paciente_ids = []
    paciente_nombres = []

    for paciente in pacientes:
        if paciente.terapeutaasignadoid != origen.id:
            raise HTTPException(
                status_code=400,
                detail=f"El paciente {paciente.id} no pertenece al terapeuta origen",
            )

        if current_user.rol == 1 and paciente.consultorioid != current_user.consultorioid:
            raise HTTPException(
                status_code=403,
                detail="No autorizado para transferir pacientes de otro consultorio.",
            )

        paciente_ids.append(paciente.id)
        paciente_nombres.append(f"{paciente.nombres} {paciente.apellidos}")

    transferencia = Transferencia(
        terapeuta_origen_id=origen.id,
        terapeuta_destino_id=destino.id,
        fecha_inicio=datetime.now(),
        activo=True,
        motivo=data.motivo,
    )

    db.add(transferencia)
    db.flush()

    for paciente in pacientes:
        db.execute(
            transferencia_paciente.insert().values(
                transferencia_id=transferencia.id,
                paciente_id=paciente.id,
            )
        )

    for paciente in pacientes:
        paciente.terapeutaasignadoid = destino.id

    if len(pacientes) == 1:
        titulo = "Paciente recibido por transferencia"
        mensaje = f"Recibiste temporalmente al paciente {paciente_nombres[0]}."
        referencia_tipo = "paciente"
        referencia_id = paciente_ids[0]
    else:
        titulo = "Pacientes recibidos por transferencia"
        mensaje = f"Recibiste temporalmente {len(pacientes)} pacientes."
        referencia_tipo = "transferencia"
        referencia_id = transferencia.id

    crear_notificacion_usuario(
        db=db,
        usuarioid=destino.id,
        titulo=titulo,
        mensaje=mensaje,
        tipo="paciente_transferido",
        referencia_tipo=referencia_tipo,
        referencia_id=referencia_id,
        data={
            "transferencia_id": transferencia.id,
            "paciente_ids": paciente_ids,
            "pacientes": paciente_nombres,
            "terapeuta_origen_id": origen.id,
            "terapeuta_destino_id": destino.id,
            "consultorioid": destino.consultorioid,
            "creado_por_id": current_user.id,
            "motivo": data.motivo,
            "actualizar": [
                "pacientes",
                "transferencias",
                "dashboard",
                "notificaciones",
            ],
        },
    )

    db.commit()
    db.refresh(transferencia)

    return transferencia


@router.post("/{transferencia_id}/revertir")
def revertir_transferencia(
    transferencia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    transferencia = (
        db.query(Transferencia)
        .options(joinedload(Transferencia.pacientes))
        .filter(
            Transferencia.id == transferencia_id,
            Transferencia.activo == True,
        )
        .first()
    )

    if not transferencia:
        raise HTTPException(
            status_code=404,
            detail="Transferencia no encontrada o ya revertida",
        )

    _validar_transferencia_para_secretario(
        current_user=current_user,
        transferencia=transferencia,
        db=db,
    )

    pacientes_ids = [p.id for p in transferencia.pacientes]

    db.query(Paciente).filter(Paciente.id.in_(pacientes_ids)).update(
        {
            Paciente.terapeutaasignadoid: transferencia.terapeuta_origen_id,
        },
        synchronize_session=False,
    )

    transferencia.activo = False
    transferencia.fecha_retorno_real = datetime.now()

    db.commit()

    return {
        "message": f"Transferencia revertida. {len(pacientes_ids)} pacientes regresaron.",
    }


@router.get("/activas", response_model=List[TransferenciaOut])
def listar_transferencias_activas(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    query = (
        db.query(Transferencia)
        .options(joinedload(Transferencia.pacientes))
        .filter(Transferencia.activo == True)
    )

    if current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        terapeutas_ids = [
            row.id
            for row in db.query(Usuario.id)
            .filter(
                Usuario.rol == 2,
                Usuario.activo == True,
                Usuario.consultorioid == current_user.consultorioid,
            )
            .all()
        ]

        query = query.filter(
            Transferencia.terapeuta_origen_id.in_(terapeutas_ids),
            Transferencia.terapeuta_destino_id.in_(terapeutas_ids),
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    return query.order_by(Transferencia.fecha_inicio.desc()).all()


@router.get("/terapeutas-con-cesiones")
def terapeutas_con_cesiones(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(Transferencia).filter(Transferencia.activo == True)

    if current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        terapeutas_ids_consultorio = [
            row.id
            for row in db.query(Usuario.id)
            .filter(
                Usuario.rol == 2,
                Usuario.activo == True,
                Usuario.consultorioid == current_user.consultorioid,
            )
            .all()
        ]

        query = query.filter(
            Transferencia.terapeuta_destino_id.in_(terapeutas_ids_consultorio)
        )

    elif current_user.rol == 2:
        query = query.filter(
            Transferencia.terapeuta_destino_id == current_user.id
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    cesiones = query.all()
    terapeutas_ids = list({t.terapeuta_destino_id for t in cesiones})

    return {
        "terapeutas_ids": terapeutas_ids,
    }