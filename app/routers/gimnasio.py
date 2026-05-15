from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_user
from ..dependencies.db import get_db
from ..models.gimnasio import MembresiaGimnasio, MovimientoGimnasio
from ..models.paciente import Paciente
from ..models.sesion_terapia import SesionTerapia
from ..models.usuario import Usuario
from ..schemas.gimnasio import (
    MembresiaGimnasioCreate,
    MembresiaGimnasioOut,
    MovimientoGimnasioCreate,
    MovimientoGimnasioOut,
    ResumenMembresiaGimnasioOut,
)

router = APIRouter(prefix="/api/gimnasio", tags=["gimnasio"])


TIPO_ASISTENCIA_GIMNASIO = 1
TIPO_TERAPIA_REEMPLAZA_GIMNASIO = 2


def _es_dia_habil(fecha: date) -> bool:
    return fecha.weekday() < 5


def _contar_dias_habiles(desde: date, hasta: date) -> int:
    if hasta < desde:
        return 0

    total = 0
    actual = desde

    while actual <= hasta:
        if _es_dia_habil(actual):
            total += 1
        actual += timedelta(days=1)

    return total


def _sumar_dias_habiles_incluyendo_inicio(inicio: date, cantidad: int) -> date:
    if cantidad <= 0:
        return inicio

    actual = inicio
    contados = 0

    while True:
        if _es_dia_habil(actual):
            contados += 1

            if contados == cantidad:
                return actual

        actual += timedelta(days=1)


def _validar_acceso_paciente(
    db: Session,
    paciente_id: int,
    current_user: Usuario,
) -> Paciente:
    paciente = db.query(Paciente).filter(Paciente.id == paciente_id).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado.",
        )

    if current_user.rol == 3:
        return paciente

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        if paciente.consultorioid != current_user.consultorioid:
            raise HTTPException(
                status_code=403,
                detail="No puedes acceder a pacientes de otro consultorio.",
            )

        return paciente

    if current_user.rol == 2:
        if paciente.terapeutaasignadoid != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="No puedes acceder a pacientes que no están asignados a ti.",
            )

        return paciente

    raise HTTPException(
        status_code=403,
        detail="No autorizado.",
    )


def _obtener_membresia_activa(
    db: Session,
    paciente_id: int,
) -> Optional[MembresiaGimnasio]:
    return (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.pacienteid == paciente_id,
            MembresiaGimnasio.activo == True,
        )
        .order_by(MembresiaGimnasio.fechainicio.desc())
        .first()
    )


def _calcular_resumen(
    db: Session,
    membresia: MembresiaGimnasio,
    fecha_referencia: Optional[date] = None,
) -> ResumenMembresiaGimnasioOut:
    hoy = fecha_referencia or date.today()

    movimientos = (
        db.query(MovimientoGimnasio)
        .filter(MovimientoGimnasio.membresiaid == membresia.id)
        .all()
    )

    dias_asistidos = sum(
        1 for m in movimientos if m.tipo == TIPO_ASISTENCIA_GIMNASIO
    )

    dias_aplazados = sum(
        1 for m in movimientos if m.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO
    )

    total_dias_programados = int(membresia.diascontratados) + dias_aplazados

    fecha_fin_estimada = _sumar_dias_habiles_incluyendo_inicio(
        membresia.fechainicio,
        total_dias_programados,
    )

    fecha_limite_calculo = min(hoy, fecha_fin_estimada)

    dias_habiles_transcurridos = _contar_dias_habiles(
        membresia.fechainicio,
        fecha_limite_calculo,
    )

    # Los días aplazados por terapia no consumen cupo de gimnasio.
    dias_consumidos = max(
        dias_habiles_transcurridos - dias_aplazados,
        0,
    )

    dias_consumidos = min(
        dias_consumidos,
        int(membresia.diascontratados),
    )

    dias_restantes = max(
        int(membresia.diascontratados) - dias_consumidos,
        0,
    )

    dias_perdidos = max(
        dias_consumidos - dias_asistidos,
        0,
    )

    movimiento_hoy = next(
    (m for m in movimientos if m.fecha == hoy),
    None,
)

    puede_registrar_hoy = (
        membresia.activo
        and _es_dia_habil(hoy)
        and dias_restantes > 0
        and hoy <= fecha_fin_estimada
        and movimiento_hoy is None
    )

    if not _es_dia_habil(hoy):
        mensaje = "Hoy no cuenta como día de gimnasio porque es fin de semana."
    elif dias_restantes <= 0:
        mensaje = "La membresía ya no tiene días disponibles."
    elif hoy > fecha_fin_estimada:
        mensaje = "La membresía ya finalizó."
    elif movimiento_hoy is not None:
        if movimiento_hoy.tipo == TIPO_ASISTENCIA_GIMNASIO:
            mensaje = "Ya se registró la asistencia de gimnasio de hoy."
        elif movimiento_hoy.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO:
            mensaje = "Hoy ya fue aplazado porque una terapia reemplazó el gimnasio."
        else:
            mensaje = "Ya existe un registro de gimnasio para hoy."
    else:
        mensaje = "La membresía está activa."

    return ResumenMembresiaGimnasioOut(
        membresia=membresia,
        fecha_fin_estimada=fecha_fin_estimada,
        dias_contratados=int(membresia.diascontratados),
        dias_habiles_transcurridos=dias_habiles_transcurridos,
        dias_asistidos=dias_asistidos,
        dias_aplazados_por_terapia=dias_aplazados,
        dias_perdidos=dias_perdidos,
        dias_consumidos=dias_consumidos,
        dias_restantes=dias_restantes,
        puede_registrar_hoy=puede_registrar_hoy,
        mensaje=mensaje,
    )


@router.post("/membresias", response_model=MembresiaGimnasioOut)
def crear_membresia_gimnasio(
    data: MembresiaGimnasioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=data.pacienteid,
        current_user=current_user,
    )

    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Solo jefe o secretario pueden crear membresías de gimnasio.",
        )

    if not _es_dia_habil(data.fechainicio):
        raise HTTPException(
            status_code=400,
            detail="La fecha de inicio debe ser de lunes a viernes.",
        )

    membresia_activa = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente.id,
    )

    if membresia_activa:
        membresia_activa.activo = False

    nueva = MembresiaGimnasio(
        pacienteid=paciente.id,
        fechainicio=data.fechainicio,
        diascontratados=data.diascontratados,
        precio=data.precio,
        activo=True,
        observaciones=data.observaciones,
    )

    db.add(nueva)
    db.commit()
    db.refresh(nueva)

    return nueva


@router.get(
    "/paciente/{paciente_id}/activa",
    response_model=Optional[ResumenMembresiaGimnasioOut],
)
def obtener_membresia_activa_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_acceso_paciente(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    membresia = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente_id,
    )

    if not membresia:
        return None

    return _calcular_resumen(
        db=db,
        membresia=membresia,
    )


@router.get(
    "/membresias/{membresia_id}/resumen",
    response_model=ResumenMembresiaGimnasioOut,
)
def obtener_resumen_membresia(
    membresia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    membresia = (
        db.query(MembresiaGimnasio)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=404,
            detail="Membresía no encontrada.",
        )

    _validar_acceso_paciente(
        db=db,
        paciente_id=membresia.pacienteid,
        current_user=current_user,
    )

    return _calcular_resumen(
        db=db,
        membresia=membresia,
    )


@router.get(
    "/membresias/{membresia_id}/movimientos",
    response_model=List[MovimientoGimnasioOut],
)
def listar_movimientos_membresia(
    membresia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    membresia = (
        db.query(MembresiaGimnasio)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=404,
            detail="Membresía no encontrada.",
        )

    _validar_acceso_paciente(
        db=db,
        paciente_id=membresia.pacienteid,
        current_user=current_user,
    )

    return (
        db.query(MovimientoGimnasio)
        .filter(MovimientoGimnasio.membresiaid == membresia.id)
        .order_by(MovimientoGimnasio.fecha.desc())
        .all()
    )


@router.post("/movimientos", response_model=MovimientoGimnasioOut)
def registrar_movimiento_gimnasio(
    data: MovimientoGimnasioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=data.pacienteid,
        current_user=current_user,
    )

    fecha_movimiento = data.fecha or date.today()

    if not _es_dia_habil(fecha_movimiento):
        raise HTTPException(
            status_code=400,
            detail="Solo se puede registrar gimnasio de lunes a viernes.",
        )

    membresia = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente.id,
    )

    if not membresia:
        raise HTTPException(
            status_code=400,
            detail="El paciente no tiene una membresía de gimnasio activa.",
        )

    resumen = _calcular_resumen(
        db=db,
        membresia=membresia,
        fecha_referencia=fecha_movimiento,
    )

    if resumen.dias_restantes <= 0:
        raise HTTPException(
            status_code=400,
            detail="La membresía ya no tiene días disponibles.",
        )

    existente = (
        db.query(MovimientoGimnasio)
        .filter(
            MovimientoGimnasio.membresiaid == membresia.id,
            MovimientoGimnasio.fecha == fecha_movimiento,
        )
        .first()
    )

    if existente:
        raise HTTPException(
            status_code=400,
            detail="Ya existe un movimiento de gimnasio registrado para esta fecha.",
        )

    if data.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO:
        if not data.sesionid:
            raise HTTPException(
                status_code=400,
                detail="Debe enviar la sesión que reemplazó el día de gimnasio.",
            )

        sesion = (
            db.query(SesionTerapia)
            .filter(SesionTerapia.id == data.sesionid)
            .first()
        )

        if not sesion:
            raise HTTPException(
                status_code=404,
                detail="Sesión no encontrada.",
            )

        if sesion.pacienteid != paciente.id:
            raise HTTPException(
                status_code=400,
                detail="La sesión no pertenece al paciente de la membresía.",
            )

    movimiento = MovimientoGimnasio(
        membresiaid=membresia.id,
        pacienteid=paciente.id,
        fecha=fecha_movimiento,
        tipo=data.tipo,
        sesionid=data.sesionid,
        tratamientopacienteid=data.tratamientopacienteid,
        observacion=data.observacion,
    )

    db.add(movimiento)
    db.commit()
    db.refresh(movimiento)

    return movimiento


@router.put("/membresias/{membresia_id}/desactivar", response_model=MembresiaGimnasioOut)
def desactivar_membresia_gimnasio(
    membresia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    membresia = (
        db.query(MembresiaGimnasio)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=404,
            detail="Membresía no encontrada.",
        )

    _validar_acceso_paciente(
        db=db,
        paciente_id=membresia.pacienteid,
        current_user=current_user,
    )

    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Solo jefe o secretario pueden desactivar membresías.",
        )

    membresia.activo = False

    db.commit()
    db.refresh(membresia)

    return membresia    