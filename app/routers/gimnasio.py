from datetime import date, timedelta, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from ..models.pago import Pago
from sqlalchemy import or_
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
    PaseDiarioGimnasioOut,
    PaseDiarioGimnasioCreate,
)

router = APIRouter(prefix="/api/gimnasio", tags=["gimnasio"])


TIPO_ASISTENCIA_GIMNASIO = 1
TIPO_TERAPIA_REEMPLAZA_GIMNASIO = 2

MODALIDAD_MENSUAL = "MENSUAL"
MODALIDAD_DIARIA = "DIARIA"

def fecha_ecuador() -> date:
    return datetime.now(timezone(timedelta(hours=-5))).date()


def now_ecuador() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))


def _es_transferencia(metodo: str) -> bool:
    return "transfer" in (metodo or "").strip().lower()

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
            MembresiaGimnasio.modalidad == MODALIDAD_MENSUAL,
        )
        .order_by(MembresiaGimnasio.fechainicio.desc())
        .first()
    )


def _calcular_resumen(
    db: Session,
    membresia: MembresiaGimnasio,
    fecha_referencia: Optional[date] = None,
) -> ResumenMembresiaGimnasioOut:
    hoy = fecha_referencia or fecha_ecuador()

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
        and hoy >= membresia.fechainicio
        and _es_dia_habil(hoy)
        and dias_restantes > 0
        and hoy <= fecha_fin_estimada
        and movimiento_hoy is None
    )

    if not _es_dia_habil(hoy):
        mensaje = "Hoy no cuenta como día de gimnasio porque es fin de semana."
    elif hoy < membresia.fechainicio:
        mensaje = "La membresía todavía no inicia."
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
        raise HTTPException(
            status_code=400,
            detail="El paciente ya tiene una membresía de gimnasio activa. Desactiva o finaliza la membresía actual antes de crear otra.",
        )

    nueva = MembresiaGimnasio(
        pacienteid=paciente.id,
        fechainicio=data.fechainicio,
        diascontratados=data.diascontratados,
        precio=data.precio,
        modalidad=MODALIDAD_MENSUAL,
        activo=True,
        observaciones=data.observaciones,
    )

    db.add(nueva)
    db.commit()
    db.refresh(nueva)

    return nueva

@router.post("/pases-diarios",response_model=PaseDiarioGimnasioOut,status_code=status.HTTP_201_CREATED,)
def registrar_pase_diario_gimnasio(
    data: PaseDiarioGimnasioCreate,
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
            detail="Solo jefe o secretario pueden registrar pases diarios de gimnasio.",
        )

    fecha_pase = data.fecha or fecha_ecuador()

    if not _es_dia_habil(fecha_pase):
        raise HTTPException(
            status_code=400,
            detail="Solo se puede registrar gimnasio de lunes a viernes.",
        )

    membresia_mensual_activa = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente.id,
    )

    if membresia_mensual_activa:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este paciente ya tiene una membresía mensual activa. "
                "Registra la asistencia desde la membresía, no como pase diario."
            ),
        )

    pase_existente = (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.pacienteid == paciente.id,
            MembresiaGimnasio.fechainicio == fecha_pase,
            MembresiaGimnasio.modalidad == MODALIDAD_DIARIA,
        )
        .first()
    )

    if pase_existente:
        raise HTTPException(
            status_code=400,
            detail="Este paciente ya tiene un pase diario registrado para esa fecha.",
        )

    movimiento_existente = (
        db.query(MovimientoGimnasio)
        .filter(
            MovimientoGimnasio.pacienteid == paciente.id,
            MovimientoGimnasio.fecha == fecha_pase,
            MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO,
        )
        .first()
    )

    if movimiento_existente:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una asistencia de gimnasio registrada para ese paciente en esa fecha.",
        )

    # Importante: el pase diario NO crea pago automáticamente.
    # Algunas personas usan el gimnasio primero y pagan después.
    # El pago se registra luego desde /api/pagos/ o /api/pagos/registrar-con-comprobante
    # usando membresiagimnasioid = pase_diario.id.
    pase_diario = MembresiaGimnasio(
        pacienteid=paciente.id,
        fechainicio=fecha_pase,
        diascontratados=1,
        precio=data.precio,
        modalidad=MODALIDAD_DIARIA,
        activo=False,
        observaciones=data.observacion,
    )

    db.add(pase_diario)
    db.flush()

    movimiento = MovimientoGimnasio(
        membresiaid=pase_diario.id,
        pacienteid=paciente.id,
        fecha=fecha_pase,
        tipo=TIPO_ASISTENCIA_GIMNASIO,
        sesionid=None,
        tratamientopacienteid=None,
        observacion=data.observacion or "Pase diario de gimnasio",
    )

    db.add(movimiento)
    db.commit()

    db.refresh(pase_diario)
    db.refresh(movimiento)

    return PaseDiarioGimnasioOut(
        paciente=f"{paciente.nombres} {paciente.apellidos}",
        membresia=pase_diario,
        movimiento=movimiento,
        pago=None,
    )

@router.get(
    "/paciente/{paciente_id}/pases-diarios",
    response_model=List[PaseDiarioGimnasioOut],
)
def listar_pases_diarios_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    pases = (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.pacienteid == paciente_id,
            MembresiaGimnasio.modalidad == MODALIDAD_DIARIA,
        )
        .order_by(
            MembresiaGimnasio.fechainicio.desc(),
            MembresiaGimnasio.id.desc(),
        )
        .all()
    )

    resultado = []

    for pase in pases:
        movimiento = (
            db.query(MovimientoGimnasio)
            .filter(
                MovimientoGimnasio.membresiaid == pase.id,
                MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO,
            )
            .order_by(MovimientoGimnasio.id.desc())
            .first()
        )

        if not movimiento:
            continue

        pago = (
            db.query(Pago)
            .filter(Pago.membresiagimnasioid == pase.id)
            .order_by(Pago.id.desc())
            .first()
        )

        resultado.append(
            PaseDiarioGimnasioOut(
                paciente=f"{paciente.nombres} {paciente.apellidos}",
                membresia=pase,
                movimiento=movimiento,
                pago=pago,
            )
        )
    return resultado

@router.get(
    "/pases-diarios",
    response_model=List[PaseDiarioGimnasioOut],
)
def listar_pases_diarios_gimnasio(
    fecha_desde: Optional[date] = Query(default=None),
    fecha_hasta: Optional[date] = Query(default=None),
    paciente_id: Optional[int] = Query(default=None),
    buscar: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(
            MembresiaGimnasio,
            MovimientoGimnasio,
            Paciente,
        )
        .join(
            MovimientoGimnasio,
            MovimientoGimnasio.membresiaid == MembresiaGimnasio.id,
        )
        .join(
            Paciente,
            Paciente.id == MembresiaGimnasio.pacienteid,
        )
        .filter(
            MembresiaGimnasio.modalidad == MODALIDAD_DIARIA,
            MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO,
        )
    )

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        query = query.filter(Paciente.consultorioid == current_user.consultorioid)

    elif current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado.",
        )

    if paciente_id is not None:
        query = query.filter(Paciente.id == paciente_id)

    if fecha_desde is not None:
        query = query.filter(MovimientoGimnasio.fecha >= fecha_desde)

    if fecha_hasta is not None:
        query = query.filter(MovimientoGimnasio.fecha <= fecha_hasta)

    if buscar and buscar.strip():
        texto = f"%{buscar.strip()}%"

        query = query.filter(
            or_(
                Paciente.nombres.ilike(texto),
                Paciente.apellidos.ilike(texto),
                MovimientoGimnasio.observacion.ilike(texto),
            )
        )

    filas = (
        query.order_by(
            MovimientoGimnasio.fecha.desc(),
            MembresiaGimnasio.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not filas:
        return []

    membresia_ids = [membresia.id for membresia, _, _ in filas]

    pagos = (
        db.query(Pago)
        .filter(Pago.membresiagimnasioid.in_(membresia_ids))
        .order_by(Pago.id.desc())
        .all()
    )

    ultimo_pago_por_membresia = {}

    for pago in pagos:
        if pago.membresiagimnasioid not in ultimo_pago_por_membresia:
            ultimo_pago_por_membresia[pago.membresiagimnasioid] = pago

    resultado = []

    for membresia, movimiento, paciente in filas:
        resultado.append(
            PaseDiarioGimnasioOut(
                paciente=f"{paciente.nombres} {paciente.apellidos}",
                membresia=membresia,
                movimiento=movimiento,
                pago=ultimo_pago_por_membresia.get(membresia.id),
            )
        )

    return resultado

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

    fecha_movimiento = data.fecha or fecha_ecuador()

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
    
    if fecha_movimiento < membresia.fechainicio:
        raise HTTPException(
            status_code=400,
            detail="No se puede registrar gimnasio antes de la fecha de inicio de la membresía.",
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