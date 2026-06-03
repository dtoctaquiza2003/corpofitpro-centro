from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Date, cast, exists, func, or_
from sqlalchemy.orm import Session, joinedload

from ..models.alerta import Alerta
from ..models.notificacion import Notificacion
from ..models.transferencia import Transferencia
from ..auth.dependencies import get_current_secretary, get_current_user
from ..dependencies.db import get_db
from ..models.consultorio import Consultorio
from ..models.gimnasio import MembresiaGimnasio
from ..models.paciente import Paciente
from ..models.pago import Pago
from ..models.sesion_terapia import SesionTerapia
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..schemas.reporte import (
    ClinicaSemanalOut,
    DashboardAccionesOut,
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

PORCENTAJE_FISIO_TERAPIA = 0.35
PORCENTAJE_CLINICA_TERAPIA = 0.65
PORCENTAJE_FISIO_GIMNASIO = 0.50
PORCENTAJE_CLINICA_GIMNASIO = 0.50

# Compatibilidad con cálculos antiguos de terapias.
PORCENTAJE_FISIO = PORCENTAJE_FISIO_TERAPIA
PORCENTAJE_CLINICA = PORCENTAJE_CLINICA_TERAPIA


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


def _es_paciente_ecuasanitas(paciente: Optional[Paciente]) -> bool:
    """Devuelve True si el paciente está cubierto por Ecuasanitas.

    Regla de negocio: Ecuasanitas SOLO aplica a terapias.
    No usar esta condición para perdonar, anular o excluir cobros de
    gimnasio mensual ni gimnasio diario; gimnasio se cobra normal.

    Usa el campo nuevo `esecuasanitas` y mantiene compatibilidad con
    pacientes antiguos que solo tenían `tiposeguro = Ecuasanitas`.
    """
    if not paciente:
        return False

    if bool(getattr(paciente, "esecuasanitas", False)):
        return True

    tipo_seguro = (getattr(paciente, "tiposeguro", None) or "").strip().lower()
    return "ecuasanitas" in tipo_seguro


def _condicion_paciente_ecuasanitas():
    return or_(
        Paciente.esecuasanitas == True,
        Paciente.tiposeguro.ilike("%ecuasanitas%"),
    )


def _columna_paciente_alerta():
    """Devuelve la columna FK del paciente en Alerta.

    En algunas versiones del modelo el atributo se llama `pacienteid`,
    y en otras `paciente_id` mapeado a la columna real `pacienteid`.
    Esto evita que el reporte falle por diferencia de nombres en el ORM.
    """
    columna = getattr(Alerta, "pacienteid", None)
    if columna is not None:
        return columna

    columna = getattr(Alerta, "paciente_id", None)
    if columna is not None:
        return columna

    raise RuntimeError("El modelo Alerta no tiene pacienteid ni paciente_id.")


def _pago_no_anulado_filter():
    return or_(Pago.anulado == False, Pago.anulado.is_(None))


def _pago_de_caja_filter():
    """Pagos que sí representan dinero cobrado dentro del sistema.

    Los pagos previos reducen saldos, pero no deben entrar al cuadre
    de caja, ingresos del día ni gráficos por método de pago.
    """
    return or_(Pago.espagoprevio == False, Pago.espagoprevio.is_(None))


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
        query.join(
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


def _resolver_consultorioid_gimnasio_para_rol(
    current_user: Usuario,
    consultorioid: Optional[int] = None,
) -> Optional[int]:
    """Aplica la misma seguridad por rol a los pagos de gimnasio."""
    return _resolver_consultorioid_para_rol(current_user, consultorioid)


def _pagos_gimnasio_por_terapeuta(
    db: Session,
    current_user: Usuario,
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
):
    """
    Pagos verificados de gimnasio mensual y pase diario agrupados por terapeuta.

    Importante: Ecuasanitas NO cubre gimnasio. Aunque el paciente sea
    Ecuasanitas, gimnasio mensual y gimnasio diario se cobran normal.

    Regla de negocio:
    - Terapia: 35% para fisioterapeuta.
    - Gimnasio mensual y diario: 50% para fisioterapeuta.

    La asignación se hace por el terapeuta principal del paciente
    (pacientes.terapeutaasignadoid), no por quien registró el pago.
    """
    _validar_filtros_para_rol(current_user, terapeutaid)
    consultorioid = _resolver_consultorioid_gimnasio_para_rol(
        current_user,
        consultorioid,
    )

    query = (
        db.query(
            Paciente.terapeutaasignadoid.label("terapeutaid"),
            Usuario.nombres.label("nombres"),
            Usuario.apellidos.label("apellidos"),
            Usuario.consultorioid.label("consultorioid"),
            func.coalesce(func.sum(Pago.monto), 0).label("total_pagado"),
        )
        .select_from(Pago)
        .join(
            MembresiaGimnasio,
            MembresiaGimnasio.id == Pago.membresiagimnasioid,
        )
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
        .join(Usuario, Usuario.id == Paciente.terapeutaasignadoid)
        .filter(
            Pago.membresiagimnasioid != None,
            Pago.estadopago == 2,
            _pago_no_anulado_filter(),
            _pago_de_caja_filter(),
            cast(Pago.fechapago, Date).between(desde, hasta),
            Paciente.terapeutaasignadoid != None,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
    )

    if current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)
    elif terapeutaid is not None:
        query = query.filter(Paciente.terapeutaasignadoid == terapeutaid)

    if consultorioid is not None:
        query = query.filter(Paciente.consultorioid == consultorioid)

    return (
        query.group_by(
            Paciente.terapeutaasignadoid,
            Usuario.nombres,
            Usuario.apellidos,
            Usuario.consultorioid,
        )
        .all()
    )


def _pagos_gimnasio_por_consultorio(
    db: Session,
    current_user: Usuario,
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
):
    """Pagos verificados de gimnasio mensual/diario agrupados por consultorio.

    Ecuasanitas no afecta esta consulta: gimnasio mensual y diario se cobran.
    """
    _validar_filtros_para_rol(current_user, terapeutaid)
    consultorioid = _resolver_consultorioid_gimnasio_para_rol(
        current_user,
        consultorioid,
    )

    query = (
        db.query(
            Paciente.consultorioid.label("consultorioid"),
            func.coalesce(func.sum(Pago.monto), 0).label("total_pagado"),
        )
        .select_from(Pago)
        .join(
            MembresiaGimnasio,
            MembresiaGimnasio.id == Pago.membresiagimnasioid,
        )
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
        .filter(
            Pago.membresiagimnasioid != None,
            Pago.estadopago == 2,
            _pago_no_anulado_filter(),
            _pago_de_caja_filter(),
            cast(Pago.fechapago, Date).between(desde, hasta),
        )
    )

    if current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)
    elif terapeutaid is not None:
        query = query.filter(Paciente.terapeutaasignadoid == terapeutaid)

    if consultorioid is not None:
        query = query.filter(Paciente.consultorioid == consultorioid)

    return query.group_by(Paciente.consultorioid).all()


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
    Calcula cuentas de tratamientos usando toda la vida del tratamiento.
    """
    tratamientos_query = db.query(TratamientoPaciente).options(
        joinedload(TratamientoPaciente.paciente)
    )

    tratamientos_query = _aplicar_filtros_tratamientos(
        tratamientos_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    if tratamiento_ids is not None:
        if not tratamiento_ids:
            return {}

        tratamientos_query = tratamientos_query.filter(
            TratamientoPaciente.id.in_(tratamiento_ids)
        )

    tratamientos = tratamientos_query.all()
    ids = {t.id for t in tratamientos}

    if not ids:
        return {}

    sesiones_por_tratamiento = dict(
        db.query(
            SesionTerapia.tratamientopacienteid,
            func.count(SesionTerapia.id),
        )
        .filter(
            SesionTerapia.tratamientopacienteid.in_(ids),
            SesionTerapia.horasalida != None,
        )
        .group_by(SesionTerapia.tratamientopacienteid)
        .all()
    )

    pagos_rows = (
        db.query(
            Pago.tratamientopacienteid,
            Pago.estadopago,
            Pago.espagoprevio,
            func.coalesce(func.sum(Pago.monto), 0),
        )
        .filter(
            Pago.tratamientopacienteid.in_(ids),
            _pago_no_anulado_filter(),
        )
        .group_by(
            Pago.tratamientopacienteid,
            Pago.estadopago,
            Pago.espagoprevio,
        )
        .all()
    )

    pagos_map: Dict[int, Dict[str, float]] = {}

    for tratamiento_id, estado, es_previo, total in pagos_rows:
        if tratamiento_id is None:
            continue

        item = pagos_map.setdefault(
            tratamiento_id,
            {
                "pagado_caja": 0.0,
                "pago_previo": 0.0,
                "pendiente_verificacion": 0.0,
            },
        )

        total_float = float(total or 0)

        if estado == 2:
            if bool(es_previo):
                item["pago_previo"] += total_float
            else:
                item["pagado_caja"] += total_float
        elif estado == 1:
            item["pendiente_verificacion"] += total_float

    result: Dict[int, Dict[str, float]] = {}

    for tratamiento in tratamientos:
        sesiones = int(sesiones_por_tratamiento.get(tratamiento.id, 0) or 0)
        precio = _precio_aplicado(tratamiento)
        total_generado = sesiones * precio
        pagos_item = pagos_map.get(
            tratamiento.id,
            {
                "pagado_caja": 0.0,
                "pago_previo": 0.0,
                "pendiente_verificacion": 0.0,
            },
        )
        pagado_caja = float(pagos_item.get("pagado_caja", 0.0) or 0.0)
        pago_previo = float(pagos_item.get("pago_previo", 0.0) or 0.0)
        pagado_verificado = pagado_caja + pago_previo
        pendiente_verificacion = float(
            pagos_item.get("pendiente_verificacion", 0.0) or 0.0
        )
        es_ecuasanitas = _es_paciente_ecuasanitas(tratamiento.paciente)
        cubierto_ecuasanitas = total_generado if es_ecuasanitas else 0.0

        # Ecuasanitas SOLO aplica a tratamientos/sesiones de terapia.
        # La deuda de terapia no se marca como pendiente del paciente.
        # La sesión sí genera comisión del 35% para el terapeuta y 65%
        # para la clínica. Gimnasio mensual/diario se cobra normal y
        # no entra en esta cuenta.
        if es_ecuasanitas:
            saldo = 0.0
            saldo_favor = max(pagado_verificado - total_generado, 0.0)
        else:
            saldo = max(total_generado - pagado_verificado, 0.0)
            saldo_favor = max(pagado_verificado - total_generado, 0.0)

        result[tratamiento.id] = {
            "precio": precio,
            "sesiones": float(sesiones),
            "total_generado": total_generado,
            "pagado_verificado": pagado_verificado,
            "pagado_caja_verificado": pagado_caja,
            "pago_previo_verificado": pago_previo,
            "pendiente_verificacion": pendiente_verificacion,
            "saldo": saldo,
            "saldo_favor": saldo_favor,
            "es_ecuasanitas": 1.0 if es_ecuasanitas else 0.0,
            "cubierto_ecuasanitas": cubierto_ecuasanitas,
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
            _pago_no_anulado_filter(),
            or_(
                Pago.espagoprevio == True,
                cast(Pago.fechapago, Date) <= hasta,
            ),
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

@router.get("/dashboard-acciones", response_model=DashboardAccionesOut)
def dashboard_acciones(
    consultorioid: Optional[int] = Query(None),
    terapeutaid: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """
    Endpoint liviano para el panel principal.
    Versión optimizada: consolida las 11 queries originales en 5
    usando COUNT con CASE condicional por tabla.
    """
    from sqlalchemy import case as sa_case

    _validar_filtros_para_rol(current_user, terapeutaid)

    hoy = date.today()
    inicio_semana_dt = datetime.combine(
        hoy - timedelta(days=hoy.weekday()), time.min
    )
    hace_7_dias = hoy - timedelta(days=7)

    consultorio_resuelto = _resolver_consultorioid_para_rol(
        current_user, consultorioid
    )

    # ------------------------------------------------------------------
    # QUERY 1 — Sesiones: hoy / en curso / finalizadas hoy
    # 3 COUNTs → 1 sola pasada con CASE
    # ------------------------------------------------------------------
    sesiones_q = db.query(
        func.count(
            sa_case(
                (SesionTerapia.fecha == hoy, 1),
            )
        ).label("hoy"),
        func.count(
            sa_case(
                (SesionTerapia.horasalida == None, 1),
            )
        ).label("en_curso"),
        func.count(
            sa_case(
                (
                    (SesionTerapia.fecha == hoy)
                    & (SesionTerapia.horasalida != None),
                    1,
                ),
            )
        ).label("finalizadas_hoy"),
    ).select_from(SesionTerapia)

    if current_user.rol == 2:
        sesiones_q = sesiones_q.filter(
            SesionTerapia.terapeutaid == current_user.id
        )
    elif terapeutaid is not None:
        sesiones_q = sesiones_q.filter(
            SesionTerapia.terapeutaid == terapeutaid
        )

    if consultorio_resuelto is not None:
        sesiones_q = sesiones_q.join(
            Paciente, Paciente.id == SesionTerapia.pacienteid
        ).filter(Paciente.consultorioid == consultorio_resuelto)

    row_sesiones = sesiones_q.one()
    sesiones_hoy = row_sesiones.hoy or 0
    sesiones_en_curso = row_sesiones.en_curso or 0
    sesiones_finalizadas_hoy = row_sesiones.finalizadas_hoy or 0

    # ------------------------------------------------------------------
    # QUERY 2 — Pacientes: activos y nuevos esta semana
    # 2 COUNTs → 1 sola pasada con CASE
    # ------------------------------------------------------------------
    pacientes_q = db.query(
        func.count(
            sa_case(
                (Paciente.estadopaciente == 1, 1),
            )
        ).label("activos"),
        func.count(
            sa_case(
                (
                    (Paciente.estadopaciente == 1)
                    & (Paciente.fechainicio >= inicio_semana_dt),
                    1,
                ),
            )
        ).label("nuevos_semana"),
    ).select_from(Paciente)

    if current_user.rol == 2:
        pacientes_q = pacientes_q.filter(
            Paciente.terapeutaasignadoid == current_user.id
        )
    elif current_user.rol == 1:
        pacientes_q = pacientes_q.filter(
            Paciente.consultorioid == consultorio_resuelto
        )
    elif current_user.rol == 3:
        if consultorioid is not None:
            pacientes_q = pacientes_q.filter(
                Paciente.consultorioid == consultorioid
            )
        if terapeutaid is not None:
            pacientes_q = pacientes_q.filter(
                Paciente.terapeutaasignadoid == terapeutaid
            )

    row_pac = pacientes_q.one()
    pacientes_activos = row_pac.activos or 0
    pacientes_nuevos_semana = row_pac.nuevos_semana or 0

    # ------------------------------------------------------------------
    # QUERY 3 — Tratamientos: activos y sin sesión en 7 días
    # 2 COUNTs → 1 sola pasada con EXISTS como subquery
    # ------------------------------------------------------------------
    sesion_reciente_exists = exists().where(
        SesionTerapia.tratamientopacienteid == TratamientoPaciente.id,
        SesionTerapia.horasalida != None,
        SesionTerapia.fecha >= hace_7_dias,
    )

    tratamientos_q = (
        db.query(
            func.count(TratamientoPaciente.id).label("activos"),
            func.count(
                sa_case(
                    (~sesion_reciente_exists, 1),
                )
            ).label("sin_sesion_7_dias"),
        )
        .select_from(TratamientoPaciente)
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
        .filter(TratamientoPaciente.activo == True)
    )

    if current_user.rol == 2:
        tratamientos_q = tratamientos_q.filter(
            Paciente.terapeutaasignadoid == current_user.id
        )
    elif terapeutaid is not None:
        tratamientos_q = tratamientos_q.filter(
            Paciente.terapeutaasignadoid == terapeutaid
        )

    if consultorio_resuelto is not None:
        tratamientos_q = tratamientos_q.filter(
            Paciente.consultorioid == consultorio_resuelto
        )

    row_trat = tratamientos_q.one()
    tratamientos_activos = row_trat.activos or 0
    tratamientos_sin_sesion_7_dias = row_trat.sin_sesion_7_dias or 0

    # ------------------------------------------------------------------
    # QUERY 4 — Pagos pendientes + alertas no leídas
    # Siguen siendo queries separadas porque tocan tablas distintas
    # con JOINs diferentes, consolidarlas no ahorraría nada.
    # ------------------------------------------------------------------
    pagos_q = (
        db.query(func.count(Pago.id))
        .join(
            TratamientoPaciente,
            TratamientoPaciente.id == Pago.tratamientopacienteid,
        )
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
        .filter(
            Pago.estadopago == 1,
            Pago.anulado == False,
            Pago.tratamientopacienteid != None,
        )
    )

    if current_user.rol == 2:
        pagos_q = pagos_q.filter(
            Paciente.terapeutaasignadoid == current_user.id
        )
    elif terapeutaid is not None:
        pagos_q = pagos_q.filter(
            Paciente.terapeutaasignadoid == terapeutaid
        )
    if consultorio_resuelto is not None:
        pagos_q = pagos_q.filter(
            Paciente.consultorioid == consultorio_resuelto
        )

    transferencias_pendientes = pagos_q.scalar() or 0

    alertas_q = (
        db.query(func.count(Alerta.id))
        .join(Paciente, Paciente.id == _columna_paciente_alerta())
        .filter(Alerta.leida == False)
    )

    if current_user.rol == 2:
        alertas_q = alertas_q.filter(
            Paciente.terapeutaasignadoid == current_user.id
        )
    elif current_user.rol == 1:
        alertas_q = alertas_q.filter(
            Paciente.consultorioid == consultorio_resuelto
        )
    elif current_user.rol == 3 and consultorioid is not None:
        alertas_q = alertas_q.filter(
            Paciente.consultorioid == consultorioid
        )

    alertas_no_leidas = alertas_q.scalar() or 0

    # ------------------------------------------------------------------
    # QUERY 5 — Notificaciones no leídas + cesiones activas
    # 2 COUNTs simples sobre tablas sin JOIN pesado → 1 query con UNION
    # es más complejo que útil; mejor 2 scalars directos.
    # ------------------------------------------------------------------
    notificaciones_no_leidas = (
        db.query(func.count(Notificacion.id))
        .filter(
            Notificacion.usuarioid == current_user.id,
            Notificacion.leida == False,
        )
        .scalar()
        or 0
    )

    cesiones_q = db.query(func.count(Transferencia.id)).filter(
        Transferencia.activo == True
    )

    if current_user.rol == 1:
        terapeutas_ids = [
            row.id
            for row in db.query(Usuario.id)
            .filter(
                Usuario.rol == 2,
                Usuario.activo == True,
                Usuario.consultorioid == consultorio_resuelto,
            )
            .all()
        ]
        if terapeutas_ids:
            cesiones_q = cesiones_q.filter(
                Transferencia.terapeuta_origen_id.in_(terapeutas_ids),
                Transferencia.terapeuta_destino_id.in_(terapeutas_ids),
            )
        else:
            cesiones_q = cesiones_q.filter(Transferencia.id == -1)
    elif current_user.rol == 2:
        cesiones_q = cesiones_q.filter(
            Transferencia.terapeuta_destino_id == current_user.id
        )

    cesiones_activas = cesiones_q.scalar() or 0

    return DashboardAccionesOut(
        sesiones_hoy=sesiones_hoy,
        sesiones_en_curso=sesiones_en_curso,
        sesiones_finalizadas_hoy=sesiones_finalizadas_hoy,
        pacientes_activos=pacientes_activos,
        pacientes_nuevos_semana=pacientes_nuevos_semana,
        tratamientos_activos=tratamientos_activos,
        tratamientos_sin_sesion_7_dias=tratamientos_sin_sesion_7_dias,
        transferencias_pendientes=transferencias_pendientes,
        alertas_no_leidas=alertas_no_leidas,
        notificaciones_no_leidas=notificaciones_no_leidas,
        cesiones_activas=cesiones_activas,
    )


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
        _pago_no_anulado_filter(),
        _pago_de_caja_filter(),
        Pago.tratamientopacienteid != None,
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
        Pago.anulado == False,
        Pago.tratamientopacienteid != None,
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
        Paciente.id == _columna_paciente_alerta(),
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
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.tratamiento_paciente),
            joinedload(SesionTerapia.paciente),
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

    sesiones = sesiones_query.all()

    tratamiento_ids = {
        s.tratamientopacienteid
        for s in sesiones
        if s.tratamientopacienteid
    }

    cuentas = _calcular_cuentas_tratamientos(
        db,
        current_user,
        tratamiento_ids=tratamiento_ids,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    total_generado = sum(
        _precio_aplicado(s.tratamiento_paciente)
        for s in sesiones
    )

    total_ecuasanitas = sum(
        _precio_aplicado(s.tratamiento_paciente)
        for s in sesiones
        if _es_paciente_ecuasanitas(s.paciente)
    )

    sesiones_ecuasanitas = sum(
        1 for s in sesiones if _es_paciente_ecuasanitas(s.paciente)
    )

    # Para no dañar el cuadre de caja, el total pagado del reporte general
    # solo suma dinero cobrado dentro del sistema. Los pagos previos se
    # muestran separado y solo reducen saldos.
    total_pagado_verificado = sum(
        item.get("pagado_caja_verificado", item["pagado_verificado"])
        for item in cuentas.values()
    )

    total_pago_previo_verificado = sum(
        item.get("pago_previo_verificado", 0.0)
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
        _pago_no_anulado_filter(),
        _pago_de_caja_filter(),
        Pago.tratamientopacienteid != None,
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

    for sesion in sesiones:
        tratamiento = sesion.tratamiento_paciente
        nombre = tratamiento.tipotratamiento if tratamiento else "Sin tratamiento"
        precio = _precio_aplicado(tratamiento)

        item = tratamiento_map.setdefault(
            nombre,
            {
                "sesiones": 0,
                "total": 0.0,
            },
        )

        item["sesiones"] += 1
        item["total"] += precio

        if sesion.fecha in dias_map:
            dias_map[sesion.fecha].sesiones += 1
            dias_map[sesion.fecha].total_generado = round(
                dias_map[sesion.fecha].total_generado + precio,
                2,
            )
            if _es_paciente_ecuasanitas(sesion.paciente):
                dias_map[sesion.fecha].cubierto_ecuasanitas = round(
                    dias_map[sesion.fecha].cubierto_ecuasanitas + precio,
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
        total_sesiones=len(sesiones),
        total_generado=round(total_generado, 2),
        total_pagado_verificado=round(total_pagado_verificado, 2),
        total_pago_previo_verificado=round(total_pago_previo_verificado, 2),
        total_ecuasanitas=round(total_ecuasanitas, 2),
        sesiones_ecuasanitas=sesiones_ecuasanitas,
        total_pendiente=round(total_pendiente, 2),
        saldo_a_favor=round(saldo_a_favor, 2),
        transferencias_pendientes=transferencias_pendientes,
        pendiente_verificacion_total=round(pendiente_verificacion_total, 2),
        por_metodo_pago=por_metodo,
        tratamientos_mas_realizados=tratamientos_mas,
        sesiones_por_dia=list(dias_map.values()),
        estado_pagos=ResumenEstadoPagosOut(
            pagado_verificado=round(total_pagado_verificado, 2),
            pago_previo=round(total_pago_previo_verificado, 2),
            pendiente_cobro=round(total_pendiente, 2),
            saldo_a_favor=round(saldo_a_favor, 2),
            pendiente_verificacion=round(pendiente_verificacion_total, 2),
            cubierto_ecuasanitas=round(total_ecuasanitas, 2),
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
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.terapeuta),
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
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

    sesiones = sesiones_query.all()

    consultorios_map = _obtener_consultorios_map(db)

    pagos_gimnasio_rows = _pagos_gimnasio_por_terapeuta(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

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

    data: Dict[int, Dict[str, float | int | str | None]] = {}
    generado_por_tratamiento_en_rango: Dict[Tuple[int, int], float] = {}

    for sesion in sesiones:
        terapeuta_id = sesion.terapeutaid
        tratamiento_id = sesion.tratamientopacienteid
        precio = _precio_aplicado(sesion.tratamiento_paciente)
        consultorio_id = sesion.paciente.consultorioid if sesion.paciente else None

        item = data.setdefault(
            terapeuta_id,
            {
                "terapeuta": _nombre_usuario(sesion.terapeuta),
                "consultorioid": consultorio_id,
                "consultorio": consultorios_map.get(
                    consultorio_id,
                    "Sin consultorio",
                ),
                "sesiones": 0,
                "total_generado": 0.0,
                "total_ecuasanitas": 0.0,
                "total_gimnasio_pagado": 0.0,
            },
        )

        item["sesiones"] = int(item["sesiones"]) + 1
        item["total_generado"] = float(item["total_generado"]) + precio

        if _es_paciente_ecuasanitas(sesion.paciente):
            item["total_ecuasanitas"] = (
                float(item.get("total_ecuasanitas", 0.0)) + precio
            )
        else:
            key = (terapeuta_id, tratamiento_id)
            generado_por_tratamiento_en_rango[key] = (
                generado_por_tratamiento_en_rango.get(key, 0.0) + precio
            )

    for row in pagos_gimnasio_rows:
        terapeuta_id = int(row.terapeutaid)
        consultorio_id = row.consultorioid
        total_gimnasio_pagado = float(row.total_pagado or 0)

        item = data.setdefault(
            terapeuta_id,
            {
                "terapeuta": f"{row.nombres or ''} {row.apellidos or ''}".strip() or "Sin terapeuta",
                "consultorioid": consultorio_id,
                "consultorio": consultorios_map.get(
                    consultorio_id,
                    "Sin consultorio",
                ),
                "sesiones": 0,
                "total_generado": 0.0,
                "total_ecuasanitas": 0.0,
                "total_gimnasio_pagado": 0.0,
            },
        )

        item["total_gimnasio_pagado"] = (
            float(item.get("total_gimnasio_pagado", 0.0)) + total_gimnasio_pagado
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
        total_terapia_generado = float(item["total_generado"])
        total_ecuasanitas = float(item.get("total_ecuasanitas", 0.0))
        total_terapia_pagado = float(pagado_por_terapeuta.get(tid, 0.0))
        total_no_ecuasanitas = max(total_terapia_generado - total_ecuasanitas, 0.0)
        pendiente_terapia = max(total_no_ecuasanitas - total_terapia_pagado, 0.0)
        total_gimnasio_pagado = float(item.get("total_gimnasio_pagado", 0.0))

        ganancia_terapia_total = total_terapia_generado * PORCENTAJE_FISIO_TERAPIA
        ganancia_terapia_ecuasanitas = total_ecuasanitas * PORCENTAJE_FISIO_TERAPIA
        ganancia_terapia_cobrada = (total_terapia_pagado + total_ecuasanitas) * PORCENTAJE_FISIO_TERAPIA
        ganancia_terapia_pendiente = pendiente_terapia * PORCENTAJE_FISIO_TERAPIA
        ganancia_gimnasio_cobrada = total_gimnasio_pagado * PORCENTAJE_FISIO_GIMNASIO

        resultado.append(
            FisioSemanalOut(
                terapeutaid=tid,
                terapeuta=str(item["terapeuta"]),
                consultorioid=item.get("consultorioid"),
                consultorio=str(item.get("consultorio") or "Sin consultorio"),
                sesiones_realizadas=int(item["sesiones"]),
                total_generado=round(total_terapia_generado, 2),
                total_pagado_pacientes=round(total_terapia_pagado, 2),
                total_pendiente_pacientes=round(pendiente_terapia, 2),
                total_ecuasanitas=round(total_ecuasanitas, 2),
                total_gimnasio_pagado=round(total_gimnasio_pagado, 2),
                ganancia_terapia_total=round(ganancia_terapia_total, 2),
                ganancia_terapia_cobrada=round(ganancia_terapia_cobrada, 2),
                ganancia_terapia_pendiente=round(ganancia_terapia_pendiente, 2),
                ganancia_terapia_ecuasanitas=round(ganancia_terapia_ecuasanitas, 2),
                ganancia_gimnasio_cobrada=round(ganancia_gimnasio_cobrada, 2),
                ganancia_fisio_total=round(ganancia_terapia_total + ganancia_gimnasio_cobrada, 2),
                ganancia_fisio_cobrada=round(ganancia_terapia_cobrada + ganancia_gimnasio_cobrada, 2),
                ganancia_fisio_pendiente=round(ganancia_terapia_pendiente, 2),
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
                "es_ecuasanitas": _es_paciente_ecuasanitas(sesion.paciente),
            },
        )

        precio = _precio_aplicado(tratamiento)
        item["sesiones"] += 1
        item["total_generado"] += precio
        if _es_paciente_ecuasanitas(sesion.paciente):
            item["es_ecuasanitas"] = True

    pacientes: List[FisioDetallePacienteOut] = []

    for (_, tratamiento_id), item in agrupado.items():
        generado = float(item["total_generado"])
        es_ecuasanitas = bool(item.get("es_ecuasanitas", False))

        if es_ecuasanitas:
            pagado = 0.0
            pendiente = 0.0
            cubierto_ecuasanitas = generado
            ganancia_cobrada = generado * PORCENTAJE_FISIO_TERAPIA
            ganancia_pendiente = 0.0
        else:
            disponible = disponible_pagado.get(tratamiento_id, 0.0)
            pagado = min(generado, disponible)
            pendiente = max(generado - pagado, 0.0)
            cubierto_ecuasanitas = 0.0
            ganancia_cobrada = pagado * PORCENTAJE_FISIO_TERAPIA
            ganancia_pendiente = pendiente * PORCENTAJE_FISIO_TERAPIA
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
                es_ecuasanitas=es_ecuasanitas,
                cubierto_ecuasanitas=round(cubierto_ecuasanitas, 2),
                ganancia_fisio=round(generado * PORCENTAJE_FISIO_TERAPIA, 2),
                ganancia_cobrada=round(ganancia_cobrada, 2),
                ganancia_pendiente=round(ganancia_pendiente, 2),
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
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
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

    sesiones = sesiones_query.all()

    consultorios_map = _obtener_consultorios_map(db)

    pagos_gimnasio_rows = _pagos_gimnasio_por_consultorio(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

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

    data: Dict[Optional[int], Dict[str, float | int | str | None]] = {}
    generado_por_clinica_tratamiento: Dict[Tuple[Optional[int], int], float] = {}

    for sesion in sesiones:
        consultorio_id = sesion.paciente.consultorioid if sesion.paciente else None
        tratamiento_id = sesion.tratamientopacienteid
        precio = _precio_aplicado(sesion.tratamiento_paciente)

        item = data.setdefault(
            consultorio_id,
            {
                "consultorioid": consultorio_id,
                "consultorio": consultorios_map.get(
                    consultorio_id,
                    "Sin consultorio",
                ),
                "sesiones": 0,
                "total_generado": 0.0,
                "total_ecuasanitas": 0.0,
                "total_gimnasio_pagado": 0.0,
            },
        )

        item["sesiones"] = int(item["sesiones"]) + 1
        item["total_generado"] = float(item["total_generado"]) + precio

        if _es_paciente_ecuasanitas(sesion.paciente):
            item["total_ecuasanitas"] = (
                float(item.get("total_ecuasanitas", 0.0)) + precio
            )
        else:
            key = (consultorio_id, tratamiento_id)
            generado_por_clinica_tratamiento[key] = (
                generado_por_clinica_tratamiento.get(key, 0.0) + precio
            )

    for row in pagos_gimnasio_rows:
        consultorio_id = row.consultorioid
        total_gimnasio_pagado = float(row.total_pagado or 0)

        item = data.setdefault(
            consultorio_id,
            {
                "consultorioid": consultorio_id,
                "consultorio": consultorios_map.get(
                    consultorio_id,
                    "Sin consultorio",
                ),
                "sesiones": 0,
                "total_generado": 0.0,
                "total_ecuasanitas": 0.0,
                "total_gimnasio_pagado": 0.0,
            },
        )

        item["total_gimnasio_pagado"] = (
            float(item.get("total_gimnasio_pagado", 0.0)) + total_gimnasio_pagado
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
        total_terapia_generado = float(item["total_generado"])
        total_ecuasanitas = float(item.get("total_ecuasanitas", 0.0))
        total_terapia_pagado = float(pagado_por_clinica.get(consultorio_id, 0.0))
        total_no_ecuasanitas = max(total_terapia_generado - total_ecuasanitas, 0.0)
        pendiente_terapia = max(total_no_ecuasanitas - total_terapia_pagado, 0.0)
        total_cobrado_o_cubierto = total_terapia_pagado + total_ecuasanitas
        total_gimnasio_pagado = float(item.get("total_gimnasio_pagado", 0.0))

        ganancia_fisios_terapia_total = total_terapia_generado * PORCENTAJE_FISIO_TERAPIA
        ganancia_fisios_terapia_cobrada = total_cobrado_o_cubierto * PORCENTAJE_FISIO_TERAPIA
        ganancia_fisios_terapia_pendiente = pendiente_terapia * PORCENTAJE_FISIO_TERAPIA
        ganancia_fisios_terapia_ecuasanitas = total_ecuasanitas * PORCENTAJE_FISIO_TERAPIA
        ganancia_clinica_terapia_total = total_terapia_generado * PORCENTAJE_CLINICA_TERAPIA
        ganancia_clinica_terapia_cobrada = total_cobrado_o_cubierto * PORCENTAJE_CLINICA_TERAPIA
        ganancia_clinica_terapia_pendiente = pendiente_terapia * PORCENTAJE_CLINICA_TERAPIA
        ganancia_clinica_terapia_ecuasanitas = total_ecuasanitas * PORCENTAJE_CLINICA_TERAPIA
        ganancia_fisios_gimnasio_cobrada = total_gimnasio_pagado * PORCENTAJE_FISIO_GIMNASIO
        ganancia_clinica_gimnasio_cobrada = total_gimnasio_pagado * PORCENTAJE_CLINICA_GIMNASIO

        resultado.append(
            ClinicaSemanalOut(
                consultorioid=consultorio_id,
                consultorio=str(item.get("consultorio") or "Sin consultorio"),
                sesiones_realizadas=int(item["sesiones"]),
                total_generado=round(total_terapia_generado, 2),
                total_pagado_pacientes=round(total_terapia_pagado, 2),
                total_pendiente_pacientes=round(pendiente_terapia, 2),
                total_ecuasanitas=round(total_ecuasanitas, 2),
                total_gimnasio_pagado=round(total_gimnasio_pagado, 2),
                ganancia_fisios_terapia_total=round(ganancia_fisios_terapia_total, 2),
                ganancia_fisios_terapia_cobrada=round(ganancia_fisios_terapia_cobrada, 2),
                ganancia_fisios_terapia_pendiente=round(ganancia_fisios_terapia_pendiente, 2),
                ganancia_fisios_terapia_ecuasanitas=round(ganancia_fisios_terapia_ecuasanitas, 2),
                ganancia_fisios_gimnasio_cobrada=round(ganancia_fisios_gimnasio_cobrada, 2),
                ganancia_clinica_terapia_total=round(ganancia_clinica_terapia_total, 2),
                ganancia_clinica_terapia_cobrada=round(ganancia_clinica_terapia_cobrada, 2),
                ganancia_clinica_terapia_pendiente=round(ganancia_clinica_terapia_pendiente, 2),
                ganancia_clinica_terapia_ecuasanitas=round(ganancia_clinica_terapia_ecuasanitas, 2),
                ganancia_clinica_gimnasio_cobrada=round(ganancia_clinica_gimnasio_cobrada, 2),
                ganancia_fisios_total=round(ganancia_fisios_terapia_total + ganancia_fisios_gimnasio_cobrada, 2),
                ganancia_fisios_cobrada=round(ganancia_fisios_terapia_cobrada + ganancia_fisios_gimnasio_cobrada, 2),
                ganancia_fisios_pendiente=round(ganancia_fisios_terapia_pendiente, 2),
                ganancia_clinica_total=round(ganancia_clinica_terapia_total + ganancia_clinica_gimnasio_cobrada, 2),
                ganancia_clinica_cobrada=round(ganancia_clinica_terapia_cobrada + ganancia_clinica_gimnasio_cobrada, 2),
                ganancia_clinica_pendiente=round(ganancia_clinica_terapia_pendiente, 2),
            )
        )

    return sorted(
        resultado,
        key=lambda item: item.total_generado,
        reverse=True,
    )