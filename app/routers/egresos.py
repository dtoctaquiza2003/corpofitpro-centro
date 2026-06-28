from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from ..auth.dependencies import get_current_user
from ..dependencies.db import get_db
from ..models.consultorio import Consultorio
from ..models.egreso import Egreso
from ..models.usuario import Usuario
from ..schemas.egreso import (
    EgresoAnularRequest,
    EgresoCreate,
    EgresoOut,
    EgresosResumenOut,
    EgresoUpdate,
)
from ..utils.fechas import now_utc, today_ecuador

router = APIRouter(prefix="/api/egresos", tags=["egresos"])


def _nombre_usuario(usuario: Optional[Usuario]) -> str:
    if not usuario:
        return "Sistema"
    return f"{usuario.nombres} {usuario.apellidos}".strip() or "Sistema"


def _nombre_consultorio(consultorio: Optional[Consultorio]) -> str:
    if not consultorio:
        return "Sin consultorio"
    return consultorio.nombre or "Sin consultorio"


def _validar_acceso_egresos(current_user: Usuario) -> None:
    # Solo secretaria y jefe registran gastos de caja.
    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Solo secretario o jefe pueden gestionar egresos.",
        )


def _resolver_consultorio_egreso(
    db: Session,
    current_user: Usuario,
    consultorioid: Optional[int],
) -> int:
    _validar_acceso_egresos(current_user)

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )
        if consultorioid is not None and consultorioid != current_user.consultorioid:
            raise HTTPException(
                status_code=403,
                detail="Un secretario solo puede registrar egresos de su consultorio.",
            )
        consultorioid = current_user.consultorioid

    if consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="Seleccione el consultorio del egreso.",
        )

    existe = db.query(Consultorio.id).filter(Consultorio.id == consultorioid).first()
    if not existe:
        raise HTTPException(status_code=404, detail="Consultorio no encontrado.")

    return int(consultorioid)


def _aplicar_filtros_egresos(
    query,
    current_user: Usuario,
    consultorioid: Optional[int] = None,
):
    _validar_acceso_egresos(current_user)

    if current_user.rol == 1:
        return query.filter(Egreso.consultorioid == current_user.consultorioid)

    if consultorioid is not None:
        return query.filter(Egreso.consultorioid == consultorioid)

    return query


def _egreso_out(row) -> EgresoOut:
    egreso, consultorio, creador, anulador = row
    return EgresoOut(
        id=int(egreso.id),
        consultorioid=int(egreso.consultorioid),
        consultorio=_nombre_consultorio(consultorio),
        fechaegreso=egreso.fechaegreso,
        categoria=egreso.categoria or "General",
        concepto=egreso.concepto or "Egreso",
        monto=round(float(egreso.monto or 0), 2),
        metodopago=egreso.metodopago or "Efectivo",
        observacion=egreso.observacion,
        creado_por_id=egreso.creado_por_id,
        creado_por=_nombre_usuario(creador),
        fechacreacion=egreso.fechacreacion,
        anulado=bool(egreso.anulado),
        motivo_anulacion=egreso.motivo_anulacion,
        fecha_anulacion=egreso.fecha_anulacion,
        anulado_por_id=egreso.anulado_por_id,
        anulado_por=_nombre_usuario(anulador) if anulador else None,
    )


@router.get("/", response_model=EgresosResumenOut)
def listar_egresos(
    desde: Optional[date] = Query(None),
    hasta: Optional[date] = Query(None),
    consultorioid: Optional[int] = Query(None),
    incluir_anulados: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_acceso_egresos(current_user)

    if desde is None:
        desde = today_ecuador().replace(day=1)
    if hasta is None:
        hasta = today_ecuador()
    if hasta < desde:
        raise HTTPException(status_code=400, detail="La fecha hasta no puede ser menor que desde.")

    creador = aliased(Usuario)
    anulador = aliased(Usuario)

    query = (
        db.query(Egreso, Consultorio, creador, anulador)
        .join(Consultorio, Consultorio.id == Egreso.consultorioid)
        .outerjoin(creador, creador.id == Egreso.creado_por_id)
        .outerjoin(anulador, anulador.id == Egreso.anulado_por_id)
        .filter(Egreso.fechaegreso >= desde, Egreso.fechaegreso <= hasta)
    )

    if not incluir_anulados:
        query = query.filter(or_(Egreso.anulado == False, Egreso.anulado.is_(None)))

    query = _aplicar_filtros_egresos(query, current_user, consultorioid)

    rows = query.order_by(Egreso.fechaegreso.desc(), Egreso.id.desc()).all()
    egresos = [_egreso_out(row) for row in rows]

    return EgresosResumenOut(
        desde=desde,
        hasta=hasta,
        total_egresos=round(sum(item.monto for item in egresos if not item.anulado), 2),
        cantidad=len([item for item in egresos if not item.anulado]),
        egresos=egresos,
    )


@router.post("/", response_model=EgresoOut)
def crear_egreso(
    data: EgresoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    consultorioid = _resolver_consultorio_egreso(db, current_user, data.consultorioid)

    egreso = Egreso(
        consultorioid=consultorioid,
        fechaegreso=data.fechaegreso,
        categoria=(data.categoria or "General").strip(),
        concepto=data.concepto.strip(),
        monto=round(float(data.monto), 2),
        metodopago=(data.metodopago or "Efectivo").strip(),
        observacion=(data.observacion or "").strip() or None,
        creado_por_id=current_user.id,
    )
    db.add(egreso)
    db.commit()
    db.refresh(egreso)

    creador = db.query(Usuario).filter(Usuario.id == egreso.creado_por_id).first()
    consultorio = db.query(Consultorio).filter(Consultorio.id == egreso.consultorioid).first()
    return _egreso_out((egreso, consultorio, creador, None))


@router.put("/{egresoid}", response_model=EgresoOut)
def actualizar_egreso(
    egresoid: int,
    data: EgresoUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_acceso_egresos(current_user)

    egreso = db.query(Egreso).filter(Egreso.id == egresoid).first()
    if not egreso:
        raise HTTPException(status_code=404, detail="Egreso no encontrado.")
    if egreso.anulado:
        raise HTTPException(status_code=400, detail="No se puede editar un egreso anulado.")

    if current_user.rol == 1 and egreso.consultorioid != current_user.consultorioid:
        raise HTTPException(status_code=403, detail="No autorizado para editar egresos de otro consultorio.")

    if data.consultorioid is not None:
        egreso.consultorioid = _resolver_consultorio_egreso(db, current_user, data.consultorioid)
    if data.fechaegreso is not None:
        egreso.fechaegreso = data.fechaegreso
    if data.categoria is not None:
        egreso.categoria = data.categoria.strip()
    if data.concepto is not None:
        egreso.concepto = data.concepto.strip()
    if data.monto is not None:
        egreso.monto = round(float(data.monto), 2)
    if data.metodopago is not None:
        egreso.metodopago = data.metodopago.strip()
    if data.observacion is not None:
        egreso.observacion = data.observacion.strip() or None

    db.commit()
    db.refresh(egreso)

    creador = db.query(Usuario).filter(Usuario.id == egreso.creado_por_id).first()
    consultorio = db.query(Consultorio).filter(Consultorio.id == egreso.consultorioid).first()
    anulador = db.query(Usuario).filter(Usuario.id == egreso.anulado_por_id).first() if egreso.anulado_por_id else None
    return _egreso_out((egreso, consultorio, creador, anulador))


@router.post("/{egresoid}/anular", response_model=EgresoOut)
def anular_egreso(
    egresoid: int,
    data: EgresoAnularRequest,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_acceso_egresos(current_user)

    egreso = db.query(Egreso).filter(Egreso.id == egresoid).first()
    if not egreso:
        raise HTTPException(status_code=404, detail="Egreso no encontrado.")
    if egreso.anulado:
        raise HTTPException(status_code=400, detail="El egreso ya está anulado.")
    if current_user.rol == 1 and egreso.consultorioid != current_user.consultorioid:
        raise HTTPException(status_code=403, detail="No autorizado para anular egresos de otro consultorio.")

    egreso.anulado = True
    egreso.motivo_anulacion = data.motivo.strip()
    egreso.fecha_anulacion = now_utc()
    egreso.anulado_por_id = current_user.id

    db.commit()
    db.refresh(egreso)

    creador = db.query(Usuario).filter(Usuario.id == egreso.creado_por_id).first()
    consultorio = db.query(Consultorio).filter(Consultorio.id == egreso.consultorioid).first()
    anulador = db.query(Usuario).filter(Usuario.id == egreso.anulado_por_id).first() if egreso.anulado_por_id else None
    return _egreso_out((egreso, consultorio, creador, anulador))
