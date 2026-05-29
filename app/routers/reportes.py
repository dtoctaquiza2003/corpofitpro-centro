import os
from datetime import date, timedelta
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Date, case, cast, func
from sqlalchemy.orm import Session, joinedload

from ..models.alerta import Alerta
from ..models.notificacion import Notificacion
from ..models.transferencia import Transferencia
from ..auth.dependencies import get_current_secretary, get_current_user
from ..dependencies.db import get_db
from ..models.consultorio import Consultorio
from ..models.paciente import Paciente
from ..models.pago import Pago
from ..models.sesion_terapia import SesionTerapia
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..schemas.reporte import (
    ClinicaSemanalOut,
    DashboardLiteOut,
    DashboardResumenOut,
    FisioDetalleOut,
    FisioDetallePacienteOut,
    FisioSemanalOut,
    MetodoPagoTotalOut,
    ReporteDiaOut,
    ReporteFiltroConsultorioOut,
    ReporteFiltroTerapeutaOut,
    ReporteFiltrosOut,
    ReporteSemanalResponse,
    ResumenEstadoPagosOut,
    SesionPorDia,
    TerapiasReporteOut,
    TratamientoRealizadoOut,
)

router = APIRouter(prefix="/api/reportes", tags=["reportes"])


# -----------------------------------------------------------------------------
# Utilidades generales
# -----------------------------------------------------------------------------

DIAS_SEMANA = [
    "Lunes",
    "Martes",
    "Miércoles",
    "Jueves",
    "Viernes",
    "Sábado",
    "Domingo",
]

PORCENTAJE_FISIO = 0.35
PORCENTAJE_CLINICA = 0.65
MAX_REPORT_DAYS = int(os.getenv("MAX_REPORT_DAYS", "31"))


def _nombre_usuario(usuario: Optional[Usuario]) -> str:
    if not usuario:
        return "Sin terapeuta"
    return f"{usuario.nombres} {usuario.apellidos}".strip()


def _nombre_paciente(paciente: Optional[Paciente]) -> str:
    if not paciente:
        return "Sin paciente"
    return f"{paciente.nombres} {paciente.apellidos}".strip()


def _nombre_consultorio(consultorio: Optional[Consultorio]) -> str:
    if not consultorio:
        return "Sin consultorio"
    return consultorio.nombre or "Sin consultorio"


def _precio_aplicado(tratamiento: Optional[TratamientoPaciente]) -> float:
    if not tratamiento or tratamiento.precio_sesion_aplicado is None:
        return 0.0
    return float(tratamiento.precio_sesion_aplicado)


def _default_range(desde: Optional[date], hasta: Optional[date]) -> tuple[date, date]:
    today = date.today()

    if desde is None:
        desde = today.replace(day=1)

    if hasta is None:
        hasta = today

    if hasta < desde:
        raise HTTPException(
            status_code=400,
            detail="La fecha hasta no puede ser menor que desde.",
        )

    dias = (hasta - desde).days + 1
    if dias > MAX_REPORT_DAYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"El rango máximo permitido para reportes es de "
                f"{MAX_REPORT_DAYS} días. Selecciona un rango menor."
            ),
        )

    return desde, hasta


def _validar_acceso_reportes(current_user: Usuario) -> None:
    if current_user.rol not in (1, 2, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )


def _validar_filtros_para_rol(
    current_user: Usuario,
    terapeutaid: Optional[int] = None,
) -> None:
    _validar_acceso_reportes(current_user)

    if current_user.rol == 2 and terapeutaid is not None and terapeutaid != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Un fisioterapeuta solo puede consultar su propio reporte.",
        )


def _resolver_consultorioid_para_rol(
    current_user: Usuario,
    consultorioid: Optional[int] = None,
) -> Optional[int]:
    """
    Resuelve el consultorio permitido según el rol.

    Secretario:
        Siempre queda limitado a current_user.consultorioid.
        Si intenta enviar otro consultorioid, se bloquea.

    Terapeuta:
        Puede quedar filtrado por su propio terapeuta.
        Si tiene consultorio y manda otro consultorio, se bloquea.

    Jefe:
        Puede consultar todo o filtrar por consultorio.
    """
    _validar_acceso_reportes(current_user)

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        if consultorioid is not None and consultorioid != current_user.consultorioid:
            raise HTTPException(
                status_code=403,
                detail="Un secretario solo puede consultar reportes de su consultorio.",
            )

        return current_user.consultorioid

    if current_user.rol == 2:
        if (
            consultorioid is not None
            and current_user.consultorioid is not None
            and consultorioid != current_user.consultorioid
        ):
            raise HTTPException(
                status_code=403,
                detail="Un fisioterapeuta solo puede consultar su propio consultorio.",
            )

        return consultorioid

    return consultorioid


def _validar_terapeuta_para_secretario(
    current_user: Usuario,
    terapeuta: Usuario,
) -> None:
    if current_user.rol != 1:
        return

    if current_user.consultorioid is None:
        raise HTTPException(
            status_code=403,
            detail="El secretario no tiene consultorio asignado.",
        )

    if terapeuta.consultorioid != current_user.consultorioid:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para consultar reportes de otro consultorio.",
        )


def _aplicar_filtros_sesiones(
    query,
    current_user: Usuario,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
):
    """Aplica rol + filtros opcionales a consultas de SesionTerapia."""
    _validar_filtros_para_rol(current_user, terapeutaid)

    consultorioid = _resolver_consultorioid_para_rol(
        current_user,
        consultorioid,
    )

    if current_user.rol == 2:
        query = query.filter(SesionTerapia.terapeutaid == current_user.id)

    elif terapeutaid is not None:
        query = query.filter(SesionTerapia.terapeutaid == terapeutaid)

    if consultorioid is not None:
        query = query.join(
            Paciente,
            Paciente.id == SesionTerapia.pacienteid,
        ).filter(
            Paciente.consultorioid == consultorioid,
        )

    return query


def _aplicar_filtros_tratamientos(
    query,
    current_user: Usuario,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
):
    """Aplica rol + filtros opcionales a consultas de TratamientoPaciente."""
    _validar_filtros_para_rol(current_user, terapeutaid)

    consultorioid = _resolver_consultorioid_para_rol(
        current_user,
        consultorioid,
    )

    query = query.join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)

    if current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)

    elif terapeutaid is not None:
        query = query.filter(Paciente.terapeutaasignadoid == terapeutaid)

    if consultorioid is not None:
        query = query.filter(Paciente.consultorioid == consultorioid)

    return query


def _aplicar_filtros_pagos(
    query,
    current_user: Usuario,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
):
    """Aplica rol + filtros opcionales a consultas de Pago."""
    _validar_filtros_para_rol(current_user, terapeutaid)

    consultorioid = _resolver_consultorioid_para_rol(
        current_user,
        consultorioid,
    )

    query = (
        query.filter(func.coalesce(Pago.anulado, False) == False)
        .join(
            TratamientoPaciente,
            TratamientoPaciente.id == Pago.tratamientopacienteid,
        )
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
    )

    if current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)

    elif terapeutaid is not None:
        query = query.filter(Paciente.terapeutaasignadoid == terapeutaid)

    if consultorioid is not None:
        query = query.filter(Paciente.consultorioid == consultorioid)

    return query


def _obtener_consultorios_map(db: Session) -> Dict[Optional[int], str]:
    rows = db.query(Consultorio).all()
    return {c.id: _nombre_consultorio(c) for c in rows}


def _generar_dias_reporte(desde: date, hasta: date) -> List[ReporteDiaOut]:
    dias: List[ReporteDiaOut] = []
    actual = desde

    while actual <= hasta:
        dias.append(
            ReporteDiaOut(
                fecha=actual,
                dia=DIAS_SEMANA[actual.weekday()],
                sesiones=0,
                total_generado=0,
                pagos_verificados=0,
            )
        )
        actual = actual + timedelta(days=1)

    return dias


# -----------------------------------------------------------------------------
# Cálculos de cuentas y pagos aplicados
# -----------------------------------------------------------------------------

def _calcular_cuentas_tratamientos(
    db: Session,
    current_user: Usuario,
    tratamiento_ids: Optional[Set[int]] = None,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
) -> Dict[int, Dict[str, float]]:
    """
    Calcula cuentas de tratamientos usando agregaciones SQL.

    Antes se cargaban tratamientos ORM + sesiones + pagos y luego se hacía el
    cálculo en Python. Con muchos pacientes esto se vuelve lento y ocupa más
    memoria. Esta versión deja que PostgreSQL agrupe los datos y devuelve solo
    totales por tratamiento.
    """
    if tratamiento_ids is not None and not tratamiento_ids:
        return {}

    tratamientos_query = db.query(
        TratamientoPaciente.id.label("id"),
        func.coalesce(TratamientoPaciente.precio_sesion_aplicado, 0).label("precio"),
    )

    tratamientos_query = _aplicar_filtros_tratamientos(
        tratamientos_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    if tratamiento_ids is not None:
        tratamientos_query = tratamientos_query.filter(
            TratamientoPaciente.id.in_(tratamiento_ids)
        )

    tratamientos_sq = tratamientos_query.subquery()

    sesiones_sq = (
        db.query(
            SesionTerapia.tratamientopacienteid.label("id"),
            func.count(SesionTerapia.id).label("sesiones"),
        )
        .join(
            tratamientos_sq,
            tratamientos_sq.c.id == SesionTerapia.tratamientopacienteid,
        )
        .filter(SesionTerapia.horasalida != None)
        .group_by(SesionTerapia.tratamientopacienteid)
        .subquery()
    )

    pagos_sq = (
        db.query(
            Pago.tratamientopacienteid.label("id"),
            func.coalesce(
                func.sum(case((Pago.estadopago == 2, Pago.monto), else_=0)),
                0,
            ).label("pagado_verificado"),
            func.coalesce(
                func.sum(case((Pago.estadopago == 1, Pago.monto), else_=0)),
                0,
            ).label("pendiente_verificacion"),
        )
        .join(
            tratamientos_sq,
            tratamientos_sq.c.id == Pago.tratamientopacienteid,
        )
        .filter(func.coalesce(Pago.anulado, False) == False)
        .group_by(Pago.tratamientopacienteid)
        .subquery()
    )

    rows = (
        db.query(
            tratamientos_sq.c.id,
            tratamientos_sq.c.precio,
            func.coalesce(sesiones_sq.c.sesiones, 0).label("sesiones"),
            func.coalesce(pagos_sq.c.pagado_verificado, 0).label("pagado_verificado"),
            func.coalesce(pagos_sq.c.pendiente_verificacion, 0).label("pendiente_verificacion"),
        )
        .outerjoin(sesiones_sq, sesiones_sq.c.id == tratamientos_sq.c.id)
        .outerjoin(pagos_sq, pagos_sq.c.id == tratamientos_sq.c.id)
        .all()
    )

    result: Dict[int, Dict[str, float]] = {}

    for row in rows:
        precio = float(row.precio or 0)
        sesiones = int(row.sesiones or 0)
        total_generado = sesiones * precio
        pagado_verificado = float(row.pagado_verificado or 0)
        pendiente_verificacion = float(row.pendiente_verificacion or 0)
        saldo = max(total_generado - pagado_verificado, 0.0)
        saldo_favor = max(pagado_verificado - total_generado, 0.0)

        result[int(row.id)] = {
            "precio": precio,
            "sesiones": float(sesiones),
            "total_generado": total_generado,
            "pagado_verificado": pagado_verificado,
            "pendiente_verificacion": pendiente_verificacion,
            "saldo": saldo,
            "saldo_favor": saldo_favor,
        }

    return result


def _pagos_aplicados_a_rango_por_tratamiento(
    db: Session,
    tratamiento_ids: Set[int],
    desde: date,
    hasta: date,
) -> Dict[int, float]:
    """
    Calcula cuánto pago verificado está disponible para cubrir sesiones del rango.
    """
    if not tratamiento_ids:
        return {}

    tratamientos = (
        db.query(TratamientoPaciente)
        .filter(TratamientoPaciente.id.in_(tratamiento_ids))
        .all()
    )

    precios = {t.id: _precio_aplicado(t) for t in tratamientos}

    sesiones_antes = dict(
        db.query(
            SesionTerapia.tratamientopacienteid,
            func.count(SesionTerapia.id),
        )
        .filter(
            SesionTerapia.tratamientopacienteid.in_(tratamiento_ids),
            SesionTerapia.horasalida != None,
            SesionTerapia.fecha < desde,
        )
        .group_by(SesionTerapia.tratamientopacienteid)
        .all()
    )

    pagos_hasta = dict(
        db.query(
            Pago.tratamientopacienteid,
            func.coalesce(func.sum(Pago.monto), 0),
        )
        .filter(
            Pago.tratamientopacienteid.in_(tratamiento_ids),
            Pago.estadopago == 2,
            func.coalesce(Pago.anulado, False) == False,
            cast(Pago.fechapago, Date) <= hasta,
        )
        .group_by(Pago.tratamientopacienteid)
        .all()
    )

    disponible_para_rango: Dict[int, float] = {}

    for tratamiento_id in tratamiento_ids:
        generado_antes = float(sesiones_antes.get(tratamiento_id, 0) or 0) * precios.get(
            tratamiento_id,
            0.0,
        )
        pagado = float(pagos_hasta.get(tratamiento_id, 0) or 0)
        disponible_para_rango[tratamiento_id] = max(pagado - generado_antes, 0.0)

    return disponible_para_rango


# -----------------------------------------------------------------------------
# Filtros para el frontend
# -----------------------------------------------------------------------------

@router.get("/filtros", response_model=ReporteFiltrosOut)
def obtener_filtros_reportes(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_acceso_reportes(current_user)

    consultorios_query = db.query(Consultorio).filter(Consultorio.activo == True)
    terapeutas_query = db.query(Usuario).filter(
        Usuario.activo == True,
        Usuario.rol == 2,
    )

    if current_user.rol == 1:
        consultorioid = _resolver_consultorioid_para_rol(current_user)

        consultorios_query = consultorios_query.filter(
            Consultorio.id == consultorioid
        )

        terapeutas_query = terapeutas_query.filter(
            Usuario.consultorioid == consultorioid
        )

    elif current_user.rol == 2:
        terapeutas_query = terapeutas_query.filter(
            Usuario.id == current_user.id
        )

        if current_user.consultorioid is not None:
            consultorios_query = consultorios_query.filter(
                Consultorio.id == current_user.consultorioid
            )

    consultorios = [
        ReporteFiltroConsultorioOut(
            id=c.id,
            nombre=_nombre_consultorio(c),
        )
        for c in consultorios_query.order_by(Consultorio.nombre).all()
    ]

    terapeutas = [
        ReporteFiltroTerapeutaOut(
            id=t.id,
            nombre=_nombre_usuario(t),
            consultorioid=t.consultorioid,
        )
        for t in terapeutas_query.order_by(
            Usuario.apellidos,
            Usuario.nombres,
        ).all()
    ]

    return ReporteFiltrosOut(
        terapeutas=terapeutas,
        consultorios=consultorios,
    )


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------

@router.get("/dashboard-resumen", response_model=DashboardResumenOut)
def dashboard_resumen(
    consultorioid: Optional[int] = Query(None),
    terapeutaid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    hoy = date.today()

    sesiones_hoy_query = db.query(SesionTerapia).filter(
        SesionTerapia.fecha == hoy
    )

    sesiones_hoy_query = _aplicar_filtros_sesiones(
        sesiones_hoy_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones_hoy = sesiones_hoy_query.count()

    pacientes_atendidos_hoy = (
        sesiones_hoy_query
        .with_entities(SesionTerapia.pacienteid)
        .distinct()
        .count()
    )

    tratamientos_activos_query = db.query(TratamientoPaciente).filter(
        TratamientoPaciente.activo == True
    )

    tratamientos_activos_query = _aplicar_filtros_tratamientos(
        tratamientos_activos_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    tratamientos_activos = tratamientos_activos_query.count()

    pagos_hoy_query = db.query(Pago).filter(
        cast(Pago.fechapago, Date) == hoy,
        Pago.estadopago == 2,
        Pago.tratamientopacienteid != None,
        func.coalesce(Pago.anulado, False) == False,
    )

    pagos_hoy_query = _aplicar_filtros_pagos(
        pagos_hoy_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    ingresos_hoy = float(
        pagos_hoy_query
        .with_entities(func.coalesce(func.sum(Pago.monto), 0))
        .scalar()
        or 0
    )

    cuentas = _calcular_cuentas_tratamientos(
        db,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    cuentas_pendientes = sum(
        1 for item in cuentas.values() if item["saldo"] > 0
    )

    saldo_pendiente_total = sum(
        item["saldo"] for item in cuentas.values()
    )

    saldo_a_favor_total = sum(
        item["saldo_favor"] for item in cuentas.values()
    )

    transferencias_pendientes_query = db.query(Pago).filter(
        Pago.estadopago == 1,
        Pago.tratamientopacienteid != None,
        func.coalesce(Pago.anulado, False) == False,
    )

    transferencias_pendientes_query = _aplicar_filtros_pagos(
        transferencias_pendientes_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    transferencias_pendientes = transferencias_pendientes_query.count()

    return DashboardResumenOut(
        sesiones_hoy=sesiones_hoy,
        pacientes_atendidos_hoy=pacientes_atendidos_hoy,
        tratamientos_activos=tratamientos_activos,
        ingresos_hoy=round(ingresos_hoy, 2),
        cuentas_pendientes=cuentas_pendientes,
        saldo_pendiente_total=round(saldo_pendiente_total, 2),
        transferencias_pendientes=transferencias_pendientes,
        saldo_a_favor_total=round(saldo_a_favor_total, 2),
    )

@router.get("/dashboard-lite", response_model=DashboardLiteOut)
def dashboard_lite(
    consultorioid: Optional[int] = Query(None),
    terapeutaid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    resumen = dashboard_resumen(
        consultorioid=consultorioid,
        terapeutaid=terapeutaid,
        db=db,
        current_user=current_user,
    )

    # Alertas no leídas
    alertas_query = db.query(Alerta).join(
        Paciente,
        Paciente.id == Alerta.paciente_id,
    ).filter(
        Alerta.leida == False,
    )

    if current_user.rol == 2:
        alertas_query = alertas_query.filter(
            Paciente.terapeutaasignadoid == current_user.id,
        )

    elif current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        alertas_query = alertas_query.filter(
            Paciente.consultorioid == current_user.consultorioid,
        )

    elif current_user.rol == 3:
        if consultorioid is not None:
            alertas_query = alertas_query.filter(
                Paciente.consultorioid == consultorioid,
            )

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    alertas_no_leidas = alertas_query.count()

    # Notificaciones no leídas del usuario actual
    notificaciones_no_leidas = (
        db.query(Notificacion)
        .filter(
            Notificacion.usuarioid == current_user.id,
            Notificacion.leida == False,
        )
        .count()
    )

    # Cesiones activas
    cesiones_query = db.query(Transferencia).filter(
        Transferencia.activo == True,
    )

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
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

        cesiones_query = cesiones_query.filter(
            Transferencia.terapeuta_origen_id.in_(terapeutas_ids),
            Transferencia.terapeuta_destino_id.in_(terapeutas_ids),
        )

    elif current_user.rol == 2:
        cesiones_query = cesiones_query.filter(
            Transferencia.terapeuta_destino_id == current_user.id,
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    cesiones_activas = cesiones_query.count()

    return DashboardLiteOut(
        sesiones_hoy=resumen.sesiones_hoy,
        pacientes_atendidos_hoy=resumen.pacientes_atendidos_hoy,
        tratamientos_activos=resumen.tratamientos_activos,
        ingresos_hoy=resumen.ingresos_hoy,
        cuentas_pendientes=resumen.cuentas_pendientes,
        saldo_pendiente_total=resumen.saldo_pendiente_total,
        transferencias_pendientes=resumen.transferencias_pendientes,
        saldo_a_favor_total=resumen.saldo_a_favor_total,
        alertas_no_leidas=alertas_no_leidas,
        notificaciones_no_leidas=notificaciones_no_leidas,
        cesiones_activas=cesiones_activas,
    )


# -----------------------------------------------------------------------------
# Reporte semanal antiguo: se mantiene por compatibilidad
# -----------------------------------------------------------------------------

@router.get("/sesiones/semana", response_model=ReporteSemanalResponse)
def reporte_semanal(
    fecha_inicio: date,
    terapeuta_id: Optional[int] = Query(None),
    consultorio_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    fecha_fin = fecha_inicio + timedelta(days=6)

    query = db.query(SesionTerapia).filter(
        SesionTerapia.fecha.between(fecha_inicio, fecha_fin)
    )

    query = _aplicar_filtros_sesiones(
        query,
        current_user,
        terapeutaid=terapeuta_id,
        consultorioid=consultorio_id,
    )

    detalle = query.options(
        joinedload(SesionTerapia.paciente),
        joinedload(SesionTerapia.terapeuta),
    ).all()

    conteo_por_dia_query = db.query(
        SesionTerapia.fecha,
        func.count(SesionTerapia.id).label("cantidad"),
    ).filter(
        SesionTerapia.fecha.between(fecha_inicio, fecha_fin)
    )

    conteo_por_dia_query = _aplicar_filtros_sesiones(
        conteo_por_dia_query,
        current_user,
        terapeutaid=terapeuta_id,
        consultorioid=consultorio_id,
    )

    conteo_por_dia = (
        conteo_por_dia_query
        .group_by(SesionTerapia.fecha)
        .all()
    )

    mapa_conteo = {item.fecha: item.cantidad for item in conteo_por_dia}

    sesiones_por_dia: List[SesionPorDia] = []

    for i, dia in enumerate(DIAS_SEMANA):
        dia_fecha = fecha_inicio + timedelta(days=i)

        sesiones_por_dia.append(
            SesionPorDia(
                dia=dia,
                fecha=dia_fecha,
                cantidad=mapa_conteo.get(dia_fecha, 0),
            )
        )

    detalle_final = [
        {
            "id": s.id,
            "fecha": s.fecha,
            "paciente": _nombre_paciente(s.paciente),
            "terapeuta": _nombre_usuario(s.terapeuta),
        }
        for s in detalle
    ]

    return {
        "fecha_inicio": fecha_inicio,
        "fecha_fin": fecha_fin,
        "sesiones_por_dia": sesiones_por_dia,
        "total_sesiones": len(detalle),
        "detalle": detalle_final,
    }


# -----------------------------------------------------------------------------
# Reporte general de terapias
# -----------------------------------------------------------------------------

@router.get("/terapias", response_model=TerapiasReporteOut)
def reporte_terapias(
    desde: Optional[date] = Query(None),
    hasta: Optional[date] = Query(None),
    terapeutaid: Optional[int] = Query(None),
    consultorioid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)

    sesiones_query = (
        db.query(
            SesionTerapia.fecha.label("fecha"),
            SesionTerapia.tratamientopacienteid.label("tratamiento_id"),
            TratamientoPaciente.tipotratamiento.label("tratamiento"),
            func.coalesce(
                TratamientoPaciente.precio_sesion_aplicado,
                0,
            ).label("precio"),
            func.count(SesionTerapia.id).label("sesiones"),
        )
        .join(
            TratamientoPaciente,
            TratamientoPaciente.id == SesionTerapia.tratamientopacienteid,
        )
        .filter(
            SesionTerapia.fecha.between(desde, hasta),
            SesionTerapia.horasalida != None,
            SesionTerapia.tratamientopacienteid != None,
        )
    )

    sesiones_query = _aplicar_filtros_sesiones(
        sesiones_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones_rows = (
        sesiones_query
        .group_by(
            SesionTerapia.fecha,
            SesionTerapia.tratamientopacienteid,
            TratamientoPaciente.tipotratamiento,
            TratamientoPaciente.precio_sesion_aplicado,
        )
        .all()
    )

    tratamiento_ids = {
        int(row.tratamiento_id)
        for row in sesiones_rows
        if row.tratamiento_id
    }

    cuentas = _calcular_cuentas_tratamientos(
        db,
        current_user,
        tratamiento_ids=tratamiento_ids,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    total_sesiones = sum(int(row.sesiones or 0) for row in sesiones_rows)
    total_generado = sum(
        int(row.sesiones or 0) * float(row.precio or 0)
        for row in sesiones_rows
    )

    total_pagado_verificado = sum(
        item["pagado_verificado"]
        for item in cuentas.values()
    )

    total_pendiente = sum(
        item["saldo"]
        for item in cuentas.values()
    )

    saldo_a_favor = sum(
        item["saldo_favor"]
        for item in cuentas.values()
    )

    pendiente_verificacion_total = sum(
        item["pendiente_verificacion"]
        for item in cuentas.values()
    )

    transferencias_pendientes = sum(
        1
        for item in cuentas.values()
        if item["pendiente_verificacion"] > 0
    )

    pagos_query = db.query(Pago).filter(
        cast(Pago.fechapago, Date).between(desde, hasta),
        Pago.estadopago == 2,
        Pago.tratamientopacienteid != None,
        func.coalesce(Pago.anulado, False) == False,
    )

    pagos_query = _aplicar_filtros_pagos(
        pagos_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    pagos_metodo_rows = (
        pagos_query
        .with_entities(
            Pago.metodopago,
            func.coalesce(func.sum(Pago.monto), 0),
        )
        .group_by(Pago.metodopago)
        .all()
    )

    por_metodo = [
        MetodoPagoTotalOut(
            metodo=row[0] or "Sin método",
            total=round(float(row[1] or 0), 2),
        )
        for row in pagos_metodo_rows
    ]

    tratamiento_map: Dict[str, Dict[str, float]] = {}

    dias_map: Dict[date, ReporteDiaOut] = {
        item.fecha: item
        for item in _generar_dias_reporte(desde, hasta)
    }

    for row in sesiones_rows:
        nombre = row.tratamiento or "Sin tratamiento"
        sesiones_cantidad = int(row.sesiones or 0)
        precio = float(row.precio or 0)
        total_row = sesiones_cantidad * precio

        item = tratamiento_map.setdefault(
            nombre,
            {
                "sesiones": 0,
                "total": 0.0,
            },
        )

        item["sesiones"] += sesiones_cantidad
        item["total"] += total_row

        if row.fecha in dias_map:
            dias_map[row.fecha].sesiones += sesiones_cantidad
            dias_map[row.fecha].total_generado = round(
                dias_map[row.fecha].total_generado + total_row,
                2,
            )

    pagos_por_dia = (
        pagos_query
        .with_entities(
            cast(Pago.fechapago, Date),
            func.coalesce(func.sum(Pago.monto), 0),
        )
        .group_by(cast(Pago.fechapago, Date))
        .all()
    )

    for fecha_pago, total in pagos_por_dia:
        if fecha_pago in dias_map:
            dias_map[fecha_pago].pagos_verificados = round(
                float(total or 0),
                2,
            )

    tratamientos_mas = sorted(
        [
            TratamientoRealizadoOut(
                tratamiento=nombre,
                sesiones=int(data["sesiones"]),
                total_generado=round(float(data["total"]), 2),
            )
            for nombre, data in tratamiento_map.items()
        ],
        key=lambda item: item.sesiones,
        reverse=True,
    )[:10]

    return TerapiasReporteOut(
        desde=desde,
        hasta=hasta,
        total_sesiones=total_sesiones,
        total_generado=round(total_generado, 2),
        total_pagado_verificado=round(total_pagado_verificado, 2),
        total_pendiente=round(total_pendiente, 2),
        saldo_a_favor=round(saldo_a_favor, 2),
        transferencias_pendientes=transferencias_pendientes,
        pendiente_verificacion_total=round(pendiente_verificacion_total, 2),
        por_metodo_pago=por_metodo,
        tratamientos_mas_realizados=tratamientos_mas,
        sesiones_por_dia=list(dias_map.values()),
        estado_pagos=ResumenEstadoPagosOut(
            pagado_verificado=round(total_pagado_verificado, 2),
            pendiente_cobro=round(total_pendiente, 2),
            saldo_a_favor=round(saldo_a_favor, 2),
            pendiente_verificacion=round(pendiente_verificacion_total, 2),
        ),
    )


# -----------------------------------------------------------------------------
# Reporte semanal de fisioterapeutas
# -----------------------------------------------------------------------------

@router.get("/fisioterapeutas-semanal", response_model=List[FisioSemanalOut])
def reporte_fisioterapeutas_semanal(
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = Query(None),
    consultorioid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)

    sesiones_query = (
        db.query(
            SesionTerapia.terapeutaid.label("terapeuta_id"),
            SesionTerapia.tratamientopacienteid.label("tratamiento_id"),
            Usuario.nombres.label("terapeuta_nombres"),
            Usuario.apellidos.label("terapeuta_apellidos"),
            Paciente.consultorioid.label("consultorio_id"),
            Consultorio.nombre.label("consultorio_nombre"),
            func.coalesce(
                TratamientoPaciente.precio_sesion_aplicado,
                0,
            ).label("precio"),
            func.count(SesionTerapia.id).label("sesiones"),
        )
        .join(Usuario, Usuario.id == SesionTerapia.terapeutaid)
        .join(Paciente, Paciente.id == SesionTerapia.pacienteid)
        .outerjoin(Consultorio, Consultorio.id == Paciente.consultorioid)
        .join(
            TratamientoPaciente,
            TratamientoPaciente.id == SesionTerapia.tratamientopacienteid,
        )
        .filter(
            SesionTerapia.fecha.between(desde, hasta),
            SesionTerapia.horasalida != None,
            SesionTerapia.tratamientopacienteid != None,
        )
    )

    _validar_filtros_para_rol(current_user, terapeutaid)
    consultorioid_resuelto = _resolver_consultorioid_para_rol(
        current_user,
        consultorioid,
    )

    if current_user.rol == 2:
        sesiones_query = sesiones_query.filter(
            SesionTerapia.terapeutaid == current_user.id
        )
    elif terapeutaid is not None:
        sesiones_query = sesiones_query.filter(
            SesionTerapia.terapeutaid == terapeutaid
        )

    if consultorioid_resuelto is not None:
        sesiones_query = sesiones_query.filter(
            Paciente.consultorioid == consultorioid_resuelto
        )

    sesiones_rows = (
        sesiones_query
        .group_by(
            SesionTerapia.terapeutaid,
            SesionTerapia.tratamientopacienteid,
            Usuario.nombres,
            Usuario.apellidos,
            Paciente.consultorioid,
            Consultorio.nombre,
            TratamientoPaciente.precio_sesion_aplicado,
        )
        .all()
    )

    tratamiento_ids = {
        int(row.tratamiento_id)
        for row in sesiones_rows
        if row.tratamiento_id
    }

    disponible_pagado = _pagos_aplicados_a_rango_por_tratamiento(
        db,
        tratamiento_ids,
        desde,
        hasta,
    )

    data: Dict[int, Dict[str, float | int | str | None]] = {}
    generado_por_tratamiento_en_rango: Dict[Tuple[int, int], float] = {}

    for row in sesiones_rows:
        terapeuta_id = int(row.terapeuta_id)
        tratamiento_id = int(row.tratamiento_id)
        sesiones_cantidad = int(row.sesiones or 0)
        precio = float(row.precio or 0)
        generado = sesiones_cantidad * precio
        consultorio_id = row.consultorio_id
        terapeuta_nombre = f"{row.terapeuta_nombres} {row.terapeuta_apellidos}".strip()

        item = data.setdefault(
            terapeuta_id,
            {
                "terapeuta": terapeuta_nombre or "Sin terapeuta",
                "consultorioid": consultorio_id,
                "consultorio": row.consultorio_nombre or "Sin consultorio",
                "sesiones": 0,
                "total_generado": 0.0,
            },
        )

        item["sesiones"] = int(item["sesiones"]) + sesiones_cantidad
        item["total_generado"] = float(item["total_generado"]) + generado

        key = (terapeuta_id, tratamiento_id)
        generado_por_tratamiento_en_rango[key] = (
            generado_por_tratamiento_en_rango.get(key, 0.0) + generado
        )

    pagado_por_terapeuta: Dict[int, float] = {
        tid: 0.0
        for tid in data.keys()
    }

    for (terapeuta_id, tratamiento_id), generado in generado_por_tratamiento_en_rango.items():
        disponible = disponible_pagado.get(tratamiento_id, 0.0)
        aplicado = min(generado, disponible)

        pagado_por_terapeuta[terapeuta_id] = (
            pagado_por_terapeuta.get(terapeuta_id, 0.0) + aplicado
        )

        disponible_pagado[tratamiento_id] = max(disponible - aplicado, 0.0)

    resultado: List[FisioSemanalOut] = []

    for tid, item in data.items():
        total_generado = float(item["total_generado"])
        total_pagado = float(pagado_por_terapeuta.get(tid, 0.0))
        pendiente = max(total_generado - total_pagado, 0.0)

        resultado.append(
            FisioSemanalOut(
                terapeutaid=tid,
                terapeuta=str(item["terapeuta"]),
                consultorioid=item.get("consultorioid"),
                consultorio=str(item.get("consultorio") or "Sin consultorio"),
                sesiones_realizadas=int(item["sesiones"]),
                total_generado=round(total_generado, 2),
                total_pagado_pacientes=round(total_pagado, 2),
                total_pendiente_pacientes=round(pendiente, 2),
                ganancia_fisio_total=round(total_generado * PORCENTAJE_FISIO, 2),
                ganancia_fisio_cobrada=round(total_pagado * PORCENTAJE_FISIO, 2),
                ganancia_fisio_pendiente=round(pendiente * PORCENTAJE_FISIO, 2),
            )
        )

    return sorted(
        resultado,
        key=lambda item: item.total_generado,
        reverse=True,
    )


@router.get("/fisioterapeutas-detalle", response_model=FisioDetalleOut)
def reporte_fisioterapeuta_detalle(
    terapeutaid: int,
    desde: date,
    hasta: date,
    consultorioid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)

    _validar_filtros_para_rol(current_user, terapeutaid)

    terapeuta = db.query(Usuario).filter(Usuario.id == terapeutaid).first()

    if not terapeuta:
        raise HTTPException(
            status_code=404,
            detail="Terapeuta no encontrado",
        )

    _validar_terapeuta_para_secretario(current_user, terapeuta)

    sesiones_query = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(
            SesionTerapia.terapeutaid == terapeutaid,
            SesionTerapia.fecha.between(desde, hasta),
            SesionTerapia.horasalida != None,
            SesionTerapia.tratamientopacienteid != None,
        )
    )

    sesiones_query = _aplicar_filtros_sesiones(
        sesiones_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones = sesiones_query.all()

    consultorios_map = _obtener_consultorios_map(db)

    tratamiento_ids = {
        s.tratamientopacienteid
        for s in sesiones
        if s.tratamientopacienteid
    }

    disponible_pagado = _pagos_aplicados_a_rango_por_tratamiento(
        db,
        tratamiento_ids,
        desde,
        hasta,
    )

    agrupado: Dict[Tuple[int, int], Dict] = {}

    for sesion in sesiones:
        tratamiento = sesion.tratamiento_paciente
        consultorio_id = sesion.paciente.consultorioid if sesion.paciente else None
        key = (sesion.pacienteid, sesion.tratamientopacienteid)

        item = agrupado.setdefault(
            key,
            {
                "pacienteid": sesion.pacienteid,
                "paciente": _nombre_paciente(sesion.paciente),
                "tratamientopacienteid": sesion.tratamientopacienteid,
                "tratamiento": tratamiento.tipotratamiento if tratamiento else "Sin tratamiento",
                "consultorioid": consultorio_id,
                "consultorio": consultorios_map.get(
                    consultorio_id,
                    "Sin consultorio",
                ),
                "sesiones": 0,
                "precio_sesion": _precio_aplicado(tratamiento),
                "total_generado": 0.0,
            },
        )

        precio = _precio_aplicado(tratamiento)
        item["sesiones"] += 1
        item["total_generado"] += precio

    pacientes: List[FisioDetallePacienteOut] = []

    for (_, tratamiento_id), item in agrupado.items():
        generado = float(item["total_generado"])
        disponible = disponible_pagado.get(tratamiento_id, 0.0)
        pagado = min(generado, disponible)
        pendiente = max(generado - pagado, 0.0)

        disponible_pagado[tratamiento_id] = max(disponible - pagado, 0.0)

        pacientes.append(
            FisioDetallePacienteOut(
                pacienteid=item["pacienteid"],
                paciente=item["paciente"],
                tratamientopacienteid=item["tratamientopacienteid"],
                tratamiento=item["tratamiento"],
                consultorioid=item["consultorioid"],
                consultorio=item["consultorio"],
                sesiones=item["sesiones"],
                precio_sesion=round(float(item["precio_sesion"]), 2),
                total_generado=round(generado, 2),
                pagado_paciente=round(pagado, 2),
                pendiente_paciente=round(pendiente, 2),
                ganancia_fisio=round(generado * PORCENTAJE_FISIO, 2),
                ganancia_cobrada=round(pagado * PORCENTAJE_FISIO, 2),
                ganancia_pendiente=round(pendiente * PORCENTAJE_FISIO, 2),
            )
        )

    return FisioDetalleOut(
        terapeutaid=terapeutaid,
        terapeuta=_nombre_usuario(terapeuta),
        desde=desde,
        hasta=hasta,
        pacientes=sorted(
            pacientes,
            key=lambda item: item.paciente,
        ),
    )


# -----------------------------------------------------------------------------
# Reporte semanal por clínicas / consultorios
# -----------------------------------------------------------------------------

@router.get("/clinicas-semanal", response_model=List[ClinicaSemanalOut])
def reporte_clinicas_semanal(
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = Query(None),
    consultorioid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)

    sesiones_query = (
        db.query(
            Paciente.consultorioid.label("consultorio_id"),
            Consultorio.nombre.label("consultorio_nombre"),
            SesionTerapia.tratamientopacienteid.label("tratamiento_id"),
            func.coalesce(
                TratamientoPaciente.precio_sesion_aplicado,
                0,
            ).label("precio"),
            func.count(SesionTerapia.id).label("sesiones"),
        )
        .join(Paciente, Paciente.id == SesionTerapia.pacienteid)
        .outerjoin(Consultorio, Consultorio.id == Paciente.consultorioid)
        .join(
            TratamientoPaciente,
            TratamientoPaciente.id == SesionTerapia.tratamientopacienteid,
        )
        .filter(
            SesionTerapia.fecha.between(desde, hasta),
            SesionTerapia.horasalida != None,
            SesionTerapia.tratamientopacienteid != None,
        )
    )

    _validar_filtros_para_rol(current_user, terapeutaid)
    consultorioid_resuelto = _resolver_consultorioid_para_rol(
        current_user,
        consultorioid,
    )

    if current_user.rol == 2:
        sesiones_query = sesiones_query.filter(
            SesionTerapia.terapeutaid == current_user.id
        )
    elif terapeutaid is not None:
        sesiones_query = sesiones_query.filter(
            SesionTerapia.terapeutaid == terapeutaid
        )

    if consultorioid_resuelto is not None:
        sesiones_query = sesiones_query.filter(
            Paciente.consultorioid == consultorioid_resuelto
        )

    sesiones_rows = (
        sesiones_query
        .group_by(
            Paciente.consultorioid,
            Consultorio.nombre,
            SesionTerapia.tratamientopacienteid,
            TratamientoPaciente.precio_sesion_aplicado,
        )
        .all()
    )

    tratamiento_ids = {
        int(row.tratamiento_id)
        for row in sesiones_rows
        if row.tratamiento_id
    }

    disponible_pagado = _pagos_aplicados_a_rango_por_tratamiento(
        db,
        tratamiento_ids,
        desde,
        hasta,
    )

    data: Dict[Optional[int], Dict[str, float | int | str | None]] = {}
    generado_por_clinica_tratamiento: Dict[Tuple[Optional[int], int], float] = {}

    for row in sesiones_rows:
        consultorio_id = row.consultorio_id
        tratamiento_id = int(row.tratamiento_id)
        sesiones_cantidad = int(row.sesiones or 0)
        precio = float(row.precio or 0)
        generado = sesiones_cantidad * precio

        item = data.setdefault(
            consultorio_id,
            {
                "consultorioid": consultorio_id,
                "consultorio": row.consultorio_nombre or "Sin consultorio",
                "sesiones": 0,
                "total_generado": 0.0,
            },
        )

        item["sesiones"] = int(item["sesiones"]) + sesiones_cantidad
        item["total_generado"] = float(item["total_generado"]) + generado

        key = (consultorio_id, tratamiento_id)
        generado_por_clinica_tratamiento[key] = (
            generado_por_clinica_tratamiento.get(key, 0.0) + generado
        )

    pagado_por_clinica: Dict[Optional[int], float] = {
        cid: 0.0
        for cid in data.keys()
    }

    for (consultorio_id, tratamiento_id), generado in generado_por_clinica_tratamiento.items():
        disponible = disponible_pagado.get(tratamiento_id, 0.0)
        aplicado = min(generado, disponible)

        pagado_por_clinica[consultorio_id] = (
            pagado_por_clinica.get(consultorio_id, 0.0) + aplicado
        )

        disponible_pagado[tratamiento_id] = max(disponible - aplicado, 0.0)

    resultado: List[ClinicaSemanalOut] = []

    for consultorio_id, item in data.items():
        total_generado = float(item["total_generado"])
        total_pagado = float(pagado_por_clinica.get(consultorio_id, 0.0))
        pendiente = max(total_generado - total_pagado, 0.0)

        resultado.append(
            ClinicaSemanalOut(
                consultorioid=consultorio_id,
                consultorio=str(item.get("consultorio") or "Sin consultorio"),
                sesiones_realizadas=int(item["sesiones"]),
                total_generado=round(total_generado, 2),
                total_pagado_pacientes=round(total_pagado, 2),
                total_pendiente_pacientes=round(pendiente, 2),
                ganancia_fisios_total=round(total_generado * PORCENTAJE_FISIO, 2),
                ganancia_fisios_cobrada=round(total_pagado * PORCENTAJE_FISIO, 2),
                ganancia_fisios_pendiente=round(pendiente * PORCENTAJE_FISIO, 2),
                ganancia_clinica_total=round(total_generado * PORCENTAJE_CLINICA, 2),
                ganancia_clinica_cobrada=round(total_pagado * PORCENTAJE_CLINICA, 2),
                ganancia_clinica_pendiente=round(pendiente * PORCENTAJE_CLINICA, 2),
            )
        )

    return sorted(
        resultado,
        key=lambda item: item.total_generado,
        reverse=True,
    )