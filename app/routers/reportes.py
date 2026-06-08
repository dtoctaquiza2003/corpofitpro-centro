from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO
from typing import Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import Date, and_, cast, exists, func, or_
from sqlalchemy.orm import Session, aliased, joinedload

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

# Terapias CORPOFIT:
# - Lunes a viernes: 35% fisioterapeuta / 65% clínica.
# - Sábado y domingo: 40% fisioterapeuta / 60% clínica.
PORCENTAJE_FISIO_TERAPIA = 0.35
PORCENTAJE_CLINICA_TERAPIA = 0.65
PORCENTAJE_FISIO_TERAPIA_FIN_SEMANA = 0.40
PORCENTAJE_CLINICA_TERAPIA_FIN_SEMANA = 0.60
PORCENTAJE_FISIO_GIMNASIO = 0.50
PORCENTAJE_CLINICA_GIMNASIO = 0.50

# Compatibilidad con cálculos antiguos de terapias.
PORCENTAJE_FISIO = PORCENTAJE_FISIO_TERAPIA
PORCENTAJE_CLINICA = PORCENTAJE_CLINICA_TERAPIA

ECUADOR_TZ = timezone(timedelta(hours=-5))


def now_ecuador() -> datetime:
    return datetime.now(ECUADOR_TZ)


def fecha_ecuador() -> date:
    return now_ecuador().date()


def _es_fin_semana(fecha: Optional[date]) -> bool:
    """True si la fecha cae sábado o domingo."""
    return bool(fecha and fecha.weekday() in {5, 6})


def porcentaje_fisio_terapia_por_fecha(fecha: Optional[date]) -> float:
    return (
        PORCENTAJE_FISIO_TERAPIA_FIN_SEMANA
        if _es_fin_semana(fecha)
        else PORCENTAJE_FISIO_TERAPIA
    )


def porcentaje_clinica_terapia_por_fecha(fecha: Optional[date]) -> float:
    return (
        PORCENTAJE_CLINICA_TERAPIA_FIN_SEMANA
        if _es_fin_semana(fecha)
        else PORCENTAJE_CLINICA_TERAPIA
    )


def ganancia_fisio_terapia(monto: float, fecha: Optional[date]) -> float:
    return float(monto or 0) * porcentaje_fisio_terapia_por_fecha(fecha)


def ganancia_clinica_terapia(monto: float, fecha: Optional[date]) -> float:
    return float(monto or 0) * porcentaje_clinica_terapia_por_fecha(fecha)

def rango_fechas_ecuador(desde: date, hasta: date) -> tuple[datetime, datetime]:
    """Devuelve [inicio, fin) del rango usando día calendario de Ecuador.

    Los pagos se guardan en UTC, pero los cortes de caja/reportes deben
    hacerse por fecha de Ecuador, sin depender de la zona horaria de Render
    ni de Supabase.
    """
    inicio = datetime.combine(desde, time.min).replace(tzinfo=ECUADOR_TZ)
    fin = datetime.combine(hasta + timedelta(days=1), time.min).replace(
        tzinfo=ECUADOR_TZ
    )
    return inicio, fin


def filtro_fechapago_ecuador(desde: date, hasta: date):
    inicio, fin = rango_fechas_ecuador(desde, hasta)
    return and_(
        Pago.fechapago >= inicio,
        Pago.fechapago < fin,
    )


def fecha_pago_ecuador_expr():
    """Fecha local de Ecuador para agrupar pagos por día."""
    return cast(func.timezone("America/Guayaquil", Pago.fechapago), Date)


def fin_dia_ecuador(fecha: date) -> datetime:
    return datetime.combine(fecha + timedelta(days=1), time.min).replace(
        tzinfo=ECUADOR_TZ
    )


def _validar_dia_semana(dia_semana: Optional[int]) -> Optional[int]:
    if dia_semana is None:
        return None
    if dia_semana < 0 or dia_semana > 6:
        raise HTTPException(
            status_code=400,
            detail="El día de la semana debe estar entre 0=Lunes y 6=Domingo.",
        )
    return dia_semana


def _aplicar_filtro_dia_sesion(query, dia_semana: Optional[int]):
    dia_semana = _validar_dia_semana(dia_semana)
    if dia_semana is None:
        return query
    return query.filter(func.extract("isodow", SesionTerapia.fecha) == dia_semana + 1)


def filtro_dia_pago_ecuador(dia_semana: Optional[int]):
    dia_semana = _validar_dia_semana(dia_semana)
    if dia_semana is None:
        return True
    return func.extract("isodow", func.timezone("America/Guayaquil", Pago.fechapago)) == dia_semana + 1


def _generar_dias_reporte_filtrados(desde: date, hasta: date, dia_semana: Optional[int]) -> List[ReporteDiaOut]:
    dias = _generar_dias_reporte(desde, hasta)
    dia_semana = _validar_dia_semana(dia_semana)
    if dia_semana is None:
        return dias
    return [item for item in dias if item.fecha.weekday() == dia_semana]


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

    Excluye pagos previos porque reducen saldos, pero no son caja actual.
    Incluye recuperación de cartera porque ese dinero se cobra hoy, aunque
    no esté asociado a una sesión/tratamiento registrado.
    """
    return or_(Pago.espagoprevio == False, Pago.espagoprevio.is_(None))


def _query_recuperacion_cartera(
    db: Session,
    current_user: Usuario,
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
    dia_semana: Optional[int] = None,
):
    """Pagos de recuperación de cartera para caja/reportes.

    No se asocian a tratamiento, sesión ni terapeuta, por eso:
    - se muestran solo como caja recuperada;
    - no se suman a productividad ni a comisión de fisioterapeuta;
    - si se filtra por terapeuta, no deben aparecer.
    """
    query = db.query(Pago).filter(
        Pago.esrecuperacioncartera == True,
        Pago.estadopago == 2,
        _pago_no_anulado_filter(),
        filtro_fechapago_ecuador(desde, hasta),
        filtro_dia_pago_ecuador(dia_semana),
    )

    if current_user.rol == 2 or terapeutaid is not None:
        return query.filter(Pago.id == -1)

    consultorio_resuelto = _resolver_consultorioid_para_rol(
        current_user,
        consultorioid,
    )

    if consultorio_resuelto is not None:
        cobrador = aliased(Usuario)
        query = query.join(
            cobrador,
            cobrador.id == Pago.creado_por_id,
        ).filter(
            cobrador.consultorioid == consultorio_resuelto,
        )

    return query



def _default_range(desde: Optional[date], hasta: Optional[date]) -> tuple[date, date]:
    today = fecha_ecuador()

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


def _sesion_finalizada_tratamiento_en_consultorio_exists(consultorioid: int):
    """
    Pacientes compartidos:
    Un tratamiento también pertenece operativamente a un consultorio si
    ya tiene sesiones finalizadas atendidas por terapeutas de ese consultorio.
    Así Atahualpa ve la sesión de un paciente del Centro si fue atendido
    por una terapeuta de Atahualpa.
    """
    terapeuta_sesion = aliased(Usuario)

    return exists().where(
        and_(
            SesionTerapia.tratamientopacienteid == TratamientoPaciente.id,
            SesionTerapia.terapeutaid == terapeuta_sesion.id,
            terapeuta_sesion.consultorioid == consultorioid,
            SesionTerapia.horasalida != None,
        )
    )


def _tratamiento_visible_para_consultorio_filter(consultorioid: int):
    """
    Visibilidad por consultorio para tratamientos/pagos de terapia.

    Incluye:
    1. Pacientes registrados en el consultorio.
    2. Pacientes de otro consultorio que ya fueron atendidos por un
       terapeuta del consultorio actual.
    """
    return or_(
        Paciente.consultorioid == consultorioid,
        _sesion_finalizada_tratamiento_en_consultorio_exists(consultorioid),
    )


def _tratamiento_visible_para_terapeuta_filter(terapeutaid: int):
    """
    Visibilidad para terapeutas:
    - Pacientes asignados directamente.
    - Pacientes compartidos/cedidos que ya tienen una sesión atendida por él.
    """
    return or_(
        Paciente.terapeutaasignadoid == terapeutaid,
        exists().where(
            and_(
                SesionTerapia.tratamientopacienteid == TratamientoPaciente.id,
                SesionTerapia.terapeutaid == terapeutaid,
                SesionTerapia.horasalida != None,
            )
        ),
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
        # Consultorio operativo: se filtra por el consultorio del terapeuta
        # que atendió, no por el consultorio de origen del paciente.
        terapeuta_sesion = aliased(Usuario)
        query = query.join(
            terapeuta_sesion,
            terapeuta_sesion.id == SesionTerapia.terapeutaid,
        ).filter(
            terapeuta_sesion.consultorioid == consultorioid,
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
        query = query.filter(
            _tratamiento_visible_para_terapeuta_filter(current_user.id)
        )

    elif terapeutaid is not None:
        query = query.filter(
            _tratamiento_visible_para_terapeuta_filter(terapeutaid)
        )

    if consultorioid is not None:
        query = query.filter(
            _tratamiento_visible_para_consultorio_filter(consultorioid)
        )

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
        query = query.filter(
            _tratamiento_visible_para_terapeuta_filter(current_user.id)
        )

    elif terapeutaid is not None:
        query = query.filter(
            _tratamiento_visible_para_terapeuta_filter(terapeutaid)
        )

    if consultorioid is not None:
        query = query.filter(
            _tratamiento_visible_para_consultorio_filter(consultorioid)
        )

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
    dia_semana: Optional[int] = None,
):
    """
    Pagos verificados de gimnasio mensual y pase diario agrupados por terapeuta.

    Importante: Ecuasanitas NO cubre gimnasio. Aunque el paciente sea
    Ecuasanitas, gimnasio mensual y gimnasio diario se cobran normal.

    Regla de negocio:
    - Terapia: 35% para fisioterapeuta de lunes a viernes; 40% sábado y domingo.
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
            filtro_fechapago_ecuador(desde, hasta),
            filtro_dia_pago_ecuador(dia_semana),
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
    dia_semana: Optional[int] = None,
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
            filtro_fechapago_ecuador(desde, hasta),
            filtro_dia_pago_ecuador(dia_semana),
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
        # La sesión sí genera comisión para el terapeuta:
        # 35% de lunes a viernes y 40% sábado/domingo. Gimnasio mensual/diario se cobra normal y
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
                Pago.fechapago < fin_dia_ecuador(hasta),
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

    hoy = fecha_ecuador()
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
        # Consultorio operativo: cuenta la sesión para el consultorio del
        # terapeuta que atendió, aunque el paciente sea de otra sede.
        terapeuta_sesion = aliased(Usuario)
        sesiones_q = sesiones_q.join(
            terapeuta_sesion,
            terapeuta_sesion.id == SesionTerapia.terapeutaid,
        ).filter(terapeuta_sesion.consultorioid == consultorio_resuelto)

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
            _tratamiento_visible_para_terapeuta_filter(current_user.id)
        )
    elif terapeutaid is not None:
        tratamientos_q = tratamientos_q.filter(
            _tratamiento_visible_para_terapeuta_filter(terapeutaid)
        )

    if consultorio_resuelto is not None:
        tratamientos_q = tratamientos_q.filter(
            _tratamiento_visible_para_consultorio_filter(consultorio_resuelto)
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
            _tratamiento_visible_para_terapeuta_filter(current_user.id)
        )
    elif terapeutaid is not None:
        pagos_q = pagos_q.filter(
            _tratamiento_visible_para_terapeuta_filter(terapeutaid)
        )
    if consultorio_resuelto is not None:
        pagos_q = pagos_q.filter(
            _tratamiento_visible_para_consultorio_filter(consultorio_resuelto)
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
    hoy = fecha_ecuador()

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
        filtro_fechapago_ecuador(hoy, hoy),
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
    dia_semana: Optional[int] = Query(None, ge=0, le=6),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)
    dia_semana = _validar_dia_semana(dia_semana)

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

    sesiones_query = _aplicar_filtro_dia_sesion(sesiones_query, dia_semana)

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
        filtro_fechapago_ecuador(desde, hasta),
        filtro_dia_pago_ecuador(dia_semana),
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
        for item in _generar_dias_reporte_filtrados(desde, hasta, dia_semana)
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

    fecha_pago_expr = fecha_pago_ecuador_expr()

    pagos_por_dia = (
        pagos_query
        .with_entities(
            fecha_pago_expr.label("fecha_pago"),
            func.coalesce(func.sum(Pago.monto), 0),
        )
        .group_by(fecha_pago_expr)
        .all()
    )

    for fecha_pago, total in pagos_por_dia:
        if fecha_pago in dias_map:
            dias_map[fecha_pago].pagos_verificados = round(
                dias_map[fecha_pago].pagos_verificados + float(total or 0),
                2,
            )

    recuperacion_cartera_query = _query_recuperacion_cartera(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
    )

    total_recuperacion_cartera = float(
        recuperacion_cartera_query
        .with_entities(func.coalesce(func.sum(Pago.monto), 0))
        .scalar()
        or 0
    )

    if total_recuperacion_cartera > 0:
        por_metodo.append(
            MetodoPagoTotalOut(
                metodo="Recuperación de cartera",
                total=round(total_recuperacion_cartera, 2),
            )
        )

    recuperacion_por_dia = (
        recuperacion_cartera_query
        .with_entities(
            fecha_pago_expr.label("fecha_pago"),
            func.coalesce(func.sum(Pago.monto), 0),
        )
        .group_by(fecha_pago_expr)
        .all()
    )

    for fecha_pago, total in recuperacion_por_dia:
        if fecha_pago in dias_map:
            dias_map[fecha_pago].pagos_verificados = round(
                dias_map[fecha_pago].pagos_verificados + float(total or 0),
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
    dia_semana: Optional[int] = Query(None, ge=0, le=6),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)
    dia_semana = _validar_dia_semana(dia_semana)

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

    sesiones_query = _aplicar_filtro_dia_sesion(sesiones_query, dia_semana)

    sesiones_query = _aplicar_filtros_sesiones(
        sesiones_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones = sesiones_query.order_by(
        SesionTerapia.fecha,
        SesionTerapia.horaingreso,
        SesionTerapia.id,
    ).all()

    consultorios_map = _obtener_consultorios_map(db)

    pagos_gimnasio_rows = _pagos_gimnasio_por_terapeuta(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
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
    sesiones_no_ecuasanitas: List[Tuple[int, int, float, date]] = []

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
                "ganancia_terapia_total": 0.0,
                "ganancia_terapia_ecuasanitas": 0.0,
            },
        )

        ganancia_fisio = ganancia_fisio_terapia(precio, sesion.fecha)
        item["sesiones"] = int(item["sesiones"]) + 1
        item["total_generado"] = float(item["total_generado"]) + precio
        item["ganancia_terapia_total"] = float(item["ganancia_terapia_total"]) + ganancia_fisio

        if _es_paciente_ecuasanitas(sesion.paciente):
            item["total_ecuasanitas"] = (
                float(item.get("total_ecuasanitas", 0.0)) + precio
            )
            item["ganancia_terapia_ecuasanitas"] = (
                float(item.get("ganancia_terapia_ecuasanitas", 0.0)) + ganancia_fisio
            )
        else:
            sesiones_no_ecuasanitas.append(
                (terapeuta_id, tratamiento_id, precio, sesion.fecha)
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
                "ganancia_terapia_total": 0.0,
                "ganancia_terapia_ecuasanitas": 0.0,
            },
        )

        item["total_gimnasio_pagado"] = (
            float(item.get("total_gimnasio_pagado", 0.0)) + total_gimnasio_pagado
        )

    pagado_por_terapeuta: Dict[int, float] = {tid: 0.0 for tid in data.keys()}
    ganancia_cobrada_no_ecuasanitas_por_terapeuta: Dict[int, float] = {tid: 0.0 for tid in data.keys()}
    ganancia_pendiente_por_terapeuta: Dict[int, float] = {tid: 0.0 for tid in data.keys()}

    # Se distribuye el pago por sesión para respetar el porcentaje de cada fecha:
    # lunes-viernes 35%, sábado-domingo 40%.
    for terapeuta_id, tratamiento_id, precio, fecha_sesion in sesiones_no_ecuasanitas:
        disponible = disponible_pagado.get(tratamiento_id, 0.0)
        aplicado = min(precio, disponible)
        pendiente = max(precio - aplicado, 0.0)
        porcentaje_fisio = porcentaje_fisio_terapia_por_fecha(fecha_sesion)

        pagado_por_terapeuta[terapeuta_id] = (
            pagado_por_terapeuta.get(terapeuta_id, 0.0) + aplicado
        )
        ganancia_cobrada_no_ecuasanitas_por_terapeuta[terapeuta_id] = (
            ganancia_cobrada_no_ecuasanitas_por_terapeuta.get(terapeuta_id, 0.0)
            + aplicado * porcentaje_fisio
        )
        ganancia_pendiente_por_terapeuta[terapeuta_id] = (
            ganancia_pendiente_por_terapeuta.get(terapeuta_id, 0.0)
            + pendiente * porcentaje_fisio
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

        ganancia_terapia_total = float(item.get("ganancia_terapia_total", 0.0))
        ganancia_terapia_ecuasanitas = float(item.get("ganancia_terapia_ecuasanitas", 0.0))
        ganancia_terapia_cobrada = (
            ganancia_cobrada_no_ecuasanitas_por_terapeuta.get(tid, 0.0)
            + ganancia_terapia_ecuasanitas
        )
        ganancia_terapia_pendiente = ganancia_pendiente_por_terapeuta.get(tid, 0.0)
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
    dia_semana: Optional[int] = Query(None, ge=0, le=6),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)
    dia_semana = _validar_dia_semana(dia_semana)

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

    sesiones_query = _aplicar_filtro_dia_sesion(sesiones_query, dia_semana)

    sesiones_query = _aplicar_filtros_sesiones(
        sesiones_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones = sesiones_query.order_by(
        SesionTerapia.fecha,
        SesionTerapia.horaingreso,
        SesionTerapia.id,
    ).all()

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
                "ganancia_total": 0.0,
                "es_ecuasanitas": _es_paciente_ecuasanitas(sesion.paciente),
                "sesiones_detalle": [],
            },
        )

        precio = _precio_aplicado(tratamiento)
        item["sesiones"] += 1
        item["total_generado"] += precio
        item["ganancia_total"] += ganancia_fisio_terapia(precio, sesion.fecha)
        item["sesiones_detalle"].append((precio, sesion.fecha))

        if _es_paciente_ecuasanitas(sesion.paciente):
            item["es_ecuasanitas"] = True

    pacientes: List[FisioDetallePacienteOut] = []

    for (_, tratamiento_id), item in agrupado.items():
        generado = float(item["total_generado"])
        ganancia_total = float(item.get("ganancia_total", 0.0))
        es_ecuasanitas = bool(item.get("es_ecuasanitas", False))

        if es_ecuasanitas:
            pagado = 0.0
            pendiente = 0.0
            cubierto_ecuasanitas = generado
            ganancia_cobrada = ganancia_total
            ganancia_pendiente = 0.0
        else:
            disponible = disponible_pagado.get(tratamiento_id, 0.0)
            pagado = 0.0
            pendiente = 0.0
            ganancia_cobrada = 0.0
            ganancia_pendiente = 0.0

            for precio, fecha_sesion in item.get("sesiones_detalle", []):
                aplicado = min(float(precio or 0), disponible)
                saldo = max(float(precio or 0) - aplicado, 0.0)
                porcentaje_fisio = porcentaje_fisio_terapia_por_fecha(fecha_sesion)

                pagado += aplicado
                pendiente += saldo
                ganancia_cobrada += aplicado * porcentaje_fisio
                ganancia_pendiente += saldo * porcentaje_fisio
                disponible = max(disponible - aplicado, 0.0)

            cubierto_ecuasanitas = 0.0
            disponible_pagado[tratamiento_id] = disponible

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
                ganancia_fisio=round(ganancia_total, 2),
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
    dia_semana: Optional[int] = Query(None, ge=0, le=6),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)
    dia_semana = _validar_dia_semana(dia_semana)

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

    sesiones_query = _aplicar_filtro_dia_sesion(sesiones_query, dia_semana)

    sesiones_query = _aplicar_filtros_sesiones(
        sesiones_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones = sesiones_query.order_by(
        SesionTerapia.fecha,
        SesionTerapia.horaingreso,
        SesionTerapia.id,
    ).all()

    consultorios_map = _obtener_consultorios_map(db)

    pagos_gimnasio_rows = _pagos_gimnasio_por_consultorio(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
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
    sesiones_no_ecuasanitas: List[Tuple[Optional[int], int, float, date]] = []

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
                "ganancia_fisios_terapia_total": 0.0,
                "ganancia_fisios_terapia_ecuasanitas": 0.0,
                "ganancia_clinica_terapia_total": 0.0,
                "ganancia_clinica_terapia_ecuasanitas": 0.0,
            },
        )

        ganancia_fisio = ganancia_fisio_terapia(precio, sesion.fecha)
        ganancia_clinica = ganancia_clinica_terapia(precio, sesion.fecha)

        item["sesiones"] = int(item["sesiones"]) + 1
        item["total_generado"] = float(item["total_generado"]) + precio
        item["ganancia_fisios_terapia_total"] = (
            float(item.get("ganancia_fisios_terapia_total", 0.0)) + ganancia_fisio
        )
        item["ganancia_clinica_terapia_total"] = (
            float(item.get("ganancia_clinica_terapia_total", 0.0)) + ganancia_clinica
        )

        if _es_paciente_ecuasanitas(sesion.paciente):
            item["total_ecuasanitas"] = (
                float(item.get("total_ecuasanitas", 0.0)) + precio
            )
            item["ganancia_fisios_terapia_ecuasanitas"] = (
                float(item.get("ganancia_fisios_terapia_ecuasanitas", 0.0)) + ganancia_fisio
            )
            item["ganancia_clinica_terapia_ecuasanitas"] = (
                float(item.get("ganancia_clinica_terapia_ecuasanitas", 0.0)) + ganancia_clinica
            )
        else:
            sesiones_no_ecuasanitas.append(
                (consultorio_id, tratamiento_id, precio, sesion.fecha)
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
                "ganancia_fisios_terapia_total": 0.0,
                "ganancia_fisios_terapia_ecuasanitas": 0.0,
                "ganancia_clinica_terapia_total": 0.0,
                "ganancia_clinica_terapia_ecuasanitas": 0.0,
            },
        )

        item["total_gimnasio_pagado"] = (
            float(item.get("total_gimnasio_pagado", 0.0)) + total_gimnasio_pagado
        )

    pagado_por_clinica: Dict[Optional[int], float] = {cid: 0.0 for cid in data.keys()}
    ganancia_fisios_cobrada_no_ecuasanitas: Dict[Optional[int], float] = {cid: 0.0 for cid in data.keys()}
    ganancia_fisios_pendiente: Dict[Optional[int], float] = {cid: 0.0 for cid in data.keys()}
    ganancia_clinica_cobrada_no_ecuasanitas: Dict[Optional[int], float] = {cid: 0.0 for cid in data.keys()}
    ganancia_clinica_pendiente: Dict[Optional[int], float] = {cid: 0.0 for cid in data.keys()}

    # Se distribuye el pago por sesión para respetar el porcentaje de cada fecha:
    # lunes-viernes 35%/65%, sábado-domingo 40%/60%.
    for consultorio_id, tratamiento_id, precio, fecha_sesion in sesiones_no_ecuasanitas:
        disponible = disponible_pagado.get(tratamiento_id, 0.0)
        aplicado = min(precio, disponible)
        pendiente = max(precio - aplicado, 0.0)
        porcentaje_fisio = porcentaje_fisio_terapia_por_fecha(fecha_sesion)
        porcentaje_clinica = porcentaje_clinica_terapia_por_fecha(fecha_sesion)

        pagado_por_clinica[consultorio_id] = (
            pagado_por_clinica.get(consultorio_id, 0.0) + aplicado
        )
        ganancia_fisios_cobrada_no_ecuasanitas[consultorio_id] = (
            ganancia_fisios_cobrada_no_ecuasanitas.get(consultorio_id, 0.0)
            + aplicado * porcentaje_fisio
        )
        ganancia_fisios_pendiente[consultorio_id] = (
            ganancia_fisios_pendiente.get(consultorio_id, 0.0)
            + pendiente * porcentaje_fisio
        )
        ganancia_clinica_cobrada_no_ecuasanitas[consultorio_id] = (
            ganancia_clinica_cobrada_no_ecuasanitas.get(consultorio_id, 0.0)
            + aplicado * porcentaje_clinica
        )
        ganancia_clinica_pendiente[consultorio_id] = (
            ganancia_clinica_pendiente.get(consultorio_id, 0.0)
            + pendiente * porcentaje_clinica
        )

        disponible_pagado[tratamiento_id] = max(disponible - aplicado, 0.0)

    resultado: List[ClinicaSemanalOut] = []

    for consultorio_id, item in data.items():
        total_terapia_generado = float(item["total_generado"])
        total_ecuasanitas = float(item.get("total_ecuasanitas", 0.0))
        total_terapia_pagado = float(pagado_por_clinica.get(consultorio_id, 0.0))
        total_no_ecuasanitas = max(total_terapia_generado - total_ecuasanitas, 0.0)
        pendiente_terapia = max(total_no_ecuasanitas - total_terapia_pagado, 0.0)
        total_gimnasio_pagado = float(item.get("total_gimnasio_pagado", 0.0))

        ganancia_fisios_terapia_total = float(item.get("ganancia_fisios_terapia_total", 0.0))
        ganancia_fisios_terapia_ecuasanitas = float(item.get("ganancia_fisios_terapia_ecuasanitas", 0.0))
        ganancia_fisios_terapia_cobrada = (
            ganancia_fisios_cobrada_no_ecuasanitas.get(consultorio_id, 0.0)
            + ganancia_fisios_terapia_ecuasanitas
        )
        ganancia_fisios_terapia_pendiente = ganancia_fisios_pendiente.get(consultorio_id, 0.0)

        ganancia_clinica_terapia_total = float(item.get("ganancia_clinica_terapia_total", 0.0))
        ganancia_clinica_terapia_ecuasanitas = float(item.get("ganancia_clinica_terapia_ecuasanitas", 0.0))
        ganancia_clinica_terapia_cobrada = (
            ganancia_clinica_cobrada_no_ecuasanitas.get(consultorio_id, 0.0)
            + ganancia_clinica_terapia_ecuasanitas
        )
        ganancia_clinica_terapia_pendiente = ganancia_clinica_pendiente.get(consultorio_id, 0.0)

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


# -----------------------------------------------------------------------------
# Exportación profesional a Excel
# -----------------------------------------------------------------------------


def _excel_bool(value: bool | None) -> str:
    return "Sí" if bool(value) else "No"


def _excel_estado_pago(estado: int | None) -> str:
    return {
        1: "Pendiente",
        2: "Verificado",
        3: "Rechazado",
    }.get(int(estado or 0), "Sin estado")


def _excel_datetime_ecuador(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    # openpyxl no acepta datetimes con zona horaria.
    return value.astimezone(ECUADOR_TZ).replace(tzinfo=None)


def _excel_fecha_desde_datetime(value: Optional[datetime]) -> Optional[date]:
    local_dt = _excel_datetime_ecuador(value)
    return local_dt.date() if local_dt else None


def _excel_hora(value) -> str:
    if value is None:
        return ""
    return value.strftime("%H:%M")


def _excel_dia(fecha_value: Optional[date]) -> str:
    if not fecha_value:
        return ""
    return DIAS_SEMANA[fecha_value.weekday()]


def _excel_safe_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _excel_nombre_persona(nombres: Optional[str], apellidos: Optional[str]) -> str:
    nombre = f"{nombres or ''} {apellidos or ''}".strip()
    return nombre or "Sin nombre"


def _excel_map_usuarios(db: Session, ids: Set[Optional[int]]) -> Dict[int, Usuario]:
    ids_limpios = {int(item) for item in ids if item is not None}
    if not ids_limpios:
        return {}
    return {u.id: u for u in db.query(Usuario).filter(Usuario.id.in_(ids_limpios)).all()}


def _excel_map_pacientes(db: Session, ids: Set[Optional[int]]) -> Dict[int, Paciente]:
    ids_limpios = {int(item) for item in ids if item is not None}
    if not ids_limpios:
        return {}
    return {p.id: p for p in db.query(Paciente).filter(Paciente.id.in_(ids_limpios)).all()}


def _excel_map_tratamientos(db: Session, ids: Set[Optional[int]]) -> Dict[int, TratamientoPaciente]:
    ids_limpios = {int(item) for item in ids if item is not None}
    if not ids_limpios:
        return {}
    return {
        t.id: t
        for t in db.query(TratamientoPaciente).filter(TratamientoPaciente.id.in_(ids_limpios)).all()
    }


def _excel_base_sesiones(
    db: Session,
    current_user: Usuario,
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
    dia_semana: Optional[int] = None,
) -> List[List]:
    sesiones_query = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.terapeuta),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(
            SesionTerapia.fecha.between(desde, hasta),
            SesionTerapia.horasalida != None,
            SesionTerapia.tratamientopacienteid != None,
        )
    )

    sesiones_query = _aplicar_filtro_dia_sesion(sesiones_query, dia_semana)
    sesiones_query = _aplicar_filtros_sesiones(
        sesiones_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )

    sesiones = sesiones_query.order_by(SesionTerapia.fecha, SesionTerapia.horaingreso).all()
    consultorios_map = _obtener_consultorios_map(db)

    rows: List[List] = []
    for sesion in sesiones:
        paciente = sesion.paciente
        terapeuta = sesion.terapeuta
        tratamiento = sesion.tratamiento_paciente
        precio = round(_precio_aplicado(tratamiento), 2)
        porcentaje_fisio = porcentaje_fisio_terapia_por_fecha(sesion.fecha)
        porcentaje_clinica = porcentaje_clinica_terapia_por_fecha(sesion.fecha)
        excel_row_number = len(rows) + 2

        rows.append(
            [
                sesion.id,
                sesion.fecha,
                _excel_dia(sesion.fecha),
                _nombre_paciente(paciente),
                _excel_safe_text(getattr(paciente, "cedula", "")),
                _nombre_usuario(terapeuta),
                consultorios_map.get(getattr(terapeuta, "consultorioid", None), "Sin consultorio"),
                consultorios_map.get(getattr(paciente, "consultorioid", None), "Sin consultorio"),
                _excel_safe_text(getattr(tratamiento, "tipotratamiento", "Sin tratamiento")),
                getattr(tratamiento, "id", None),
                _excel_hora(sesion.horaingreso),
                _excel_hora(sesion.horasalida),
                sesion.duracionminutos or 0,
                sesion.escaladolorentrada,
                sesion.escaladolorsalida,
                precio,
                _excel_bool(_es_paciente_ecuasanitas(paciente)),
                porcentaje_fisio,
                porcentaje_clinica,
                f"=ROUND(P{excel_row_number}*R{excel_row_number},2)",
                f"=ROUND(P{excel_row_number}*S{excel_row_number},2)",
            ]
        )

    return rows


def _excel_base_pagos(
    db: Session,
    current_user: Usuario,
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
    dia_semana: Optional[int] = None,
) -> List[List]:
    consultorios_map = _obtener_consultorios_map(db)
    rows: List[List] = []

    # Pagos de terapias: incluye verificados, pendientes, rechazados, previos y anulados
    # para auditoría. La columna "Caja válida" separa lo que sí entra al cuadre.
    pagos_terapia_query = db.query(Pago).filter(
        Pago.tratamientopacienteid != None,
        filtro_fechapago_ecuador(desde, hasta),
        filtro_dia_pago_ecuador(dia_semana),
    )
    pagos_terapia_query = _aplicar_filtros_pagos(
        pagos_terapia_query,
        current_user,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
    )
    pagos_terapia = pagos_terapia_query.order_by(Pago.fechapago, Pago.id).all()

    tratamiento_map = _excel_map_tratamientos(
        db,
        {p.tratamientopacienteid for p in pagos_terapia},
    )
    pacientes_map = _excel_map_pacientes(
        db,
        {p.pacienteid for p in pagos_terapia}
        | {t.pacienteid for t in tratamiento_map.values() if t is not None},
    )
    usuarios_map = _excel_map_usuarios(
        db,
        {getattr(pacientes_map.get(p.pacienteid), "terapeutaasignadoid", None) for p in pagos_terapia}
        | {p.creado_por_id for p in pagos_terapia},
    )

    def add_pago_row(
        pago: Pago,
        paciente: Optional[Paciente],
        responsable: str,
        consultorio_nombre: str,
        tipo: str,
        referencia: str,
        observacion: str = "",
    ) -> None:
        fecha_pago = _excel_fecha_desde_datetime(pago.fechapago)
        excel_row_number = len(rows) + 2
        rows.append(
            [
                pago.id,
                fecha_pago,
                _excel_dia(fecha_pago),
                _nombre_paciente(paciente),
                _excel_safe_text(getattr(paciente, "cedula", "")),
                responsable,
                consultorio_nombre,
                tipo,
                _excel_safe_text(pago.metodopago) or "Sin método",
                _excel_estado_pago(pago.estadopago),
                round(float(pago.monto or 0), 2),
                _excel_bool(pago.espagoprevio),
                _excel_bool(pago.esrecuperacioncartera),
                _excel_bool(pago.anulado),
                referencia,
                observacion,
                pago.creado_por_id,
                f'=IF(AND(J{excel_row_number}="Verificado",L{excel_row_number}="No",N{excel_row_number}="No"),K{excel_row_number},0)',
            ]
        )

    for pago in pagos_terapia:
        tratamiento = tratamiento_map.get(pago.tratamientopacienteid)
        paciente = pacientes_map.get(pago.pacienteid) or pacientes_map.get(getattr(tratamiento, "pacienteid", None))
        terapeuta = usuarios_map.get(getattr(paciente, "terapeutaasignadoid", None))
        referencia = _excel_safe_text(getattr(tratamiento, "tipotratamiento", "Terapia"))
        observacion = _excel_safe_text(pago.observacionpagoprevio or pago.motivo_rechazo or pago.motivo_anulacion)
        add_pago_row(
            pago=pago,
            paciente=paciente,
            responsable=_nombre_usuario(terapeuta),
            consultorio_nombre=consultorios_map.get(getattr(paciente, "consultorioid", None), "Sin consultorio"),
            tipo="Terapia",
            referencia=referencia,
            observacion=observacion,
        )

    # Pagos de gimnasio mensual / diario. Gimnasio no se cubre por Ecuasanitas.
    consultorio_resuelto = _resolver_consultorioid_gimnasio_para_rol(current_user, consultorioid)
    pagos_gimnasio_query = (
        db.query(Pago)
        .join(MembresiaGimnasio, MembresiaGimnasio.id == Pago.membresiagimnasioid)
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
        .filter(
            Pago.membresiagimnasioid != None,
            filtro_fechapago_ecuador(desde, hasta),
            filtro_dia_pago_ecuador(dia_semana),
        )
    )

    if current_user.rol == 2:
        pagos_gimnasio_query = pagos_gimnasio_query.filter(Paciente.terapeutaasignadoid == current_user.id)
    elif terapeutaid is not None:
        pagos_gimnasio_query = pagos_gimnasio_query.filter(Paciente.terapeutaasignadoid == terapeutaid)

    if consultorio_resuelto is not None:
        pagos_gimnasio_query = pagos_gimnasio_query.filter(Paciente.consultorioid == consultorio_resuelto)

    pagos_gimnasio = pagos_gimnasio_query.order_by(Pago.fechapago, Pago.id).all()
    pacientes_gym = _excel_map_pacientes(db, {p.pacienteid for p in pagos_gimnasio})
    usuarios_gym = _excel_map_usuarios(
        db,
        {getattr(pacientes_gym.get(p.pacienteid), "terapeutaasignadoid", None) for p in pagos_gimnasio}
        | {p.creado_por_id for p in pagos_gimnasio},
    )

    for pago in pagos_gimnasio:
        paciente = pacientes_gym.get(pago.pacienteid)
        terapeuta = usuarios_gym.get(getattr(paciente, "terapeutaasignadoid", None))
        observacion = _excel_safe_text(pago.observacionpagoprevio or pago.motivo_rechazo or pago.motivo_anulacion)
        add_pago_row(
            pago=pago,
            paciente=paciente,
            responsable=_nombre_usuario(terapeuta),
            consultorio_nombre=consultorios_map.get(getattr(paciente, "consultorioid", None), "Sin consultorio"),
            tipo="Gimnasio",
            referencia=f"Membresía/Pase #{pago.membresiagimnasioid}",
            observacion=observacion,
        )

    # Recuperación de cartera: entra a caja, pero no reduce saldos ni genera comisión automática.
    recuperacion_query = _query_recuperacion_cartera(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
    )
    pagos_recuperacion = recuperacion_query.order_by(Pago.fechapago, Pago.id).all()
    pacientes_rec = _excel_map_pacientes(db, {p.pacienteid for p in pagos_recuperacion})
    cobradores_rec = _excel_map_usuarios(db, {p.creado_por_id for p in pagos_recuperacion})

    for pago in pagos_recuperacion:
        paciente = pacientes_rec.get(pago.pacienteid)
        cobrador = cobradores_rec.get(pago.creado_por_id)
        add_pago_row(
            pago=pago,
            paciente=paciente,
            responsable=f"Cobrador: {_nombre_usuario(cobrador)}",
            consultorio_nombre=consultorios_map.get(getattr(cobrador, "consultorioid", None), "Sin consultorio"),
            tipo="Recuperación cartera",
            referencia="Cobro anterior al sistema",
            observacion=_excel_safe_text(pago.observacion_cartera),
        )

    rows.sort(key=lambda row: (row[1] or date.min, row[0] or 0))
    return rows


def _excel_aplicar_estilo_workbook(wb) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill, Side, Border
    from openpyxl.utils import get_column_letter

    azul = "1F4E78"
    azul_claro = "D9EAF7"
    gris = "F4F6F8"
    verde = "E2F0D9"
    blanco = "FFFFFF"
    borde = Border(
        left=Side(style="thin", color="D9E2F3"),
        right=Side(style="thin", color="D9E2F3"),
        top=Side(style="thin", color="D9E2F3"),
        bottom=Side(style="thin", color="D9E2F3"),
    )

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = Alignment(vertical="center")
                if cell.value is not None:
                    cell.border = borde

        for cell in ws[1]:
            if cell.value:
                cell.font = Font(bold=True, color=blanco, size=12)
                cell.fill = PatternFill("solid", fgColor=azul)
                cell.alignment = Alignment(horizontal="center", vertical="center")

        # Encabezados de tablas/secciones.
        for row in ws.iter_rows():
            first = row[0].value if row else None
            if isinstance(first, str) and first.startswith("▶"):
                for cell in row:
                    if cell.value:
                        cell.font = Font(bold=True, color="12355B", size=12)
                        cell.fill = PatternFill("solid", fgColor=azul_claro)

        for col_idx in range(1, ws.max_column + 1):
            max_len = 0
            letter = get_column_letter(col_idx)
            for cell in ws.iter_cols(min_col=col_idx, max_col=col_idx, min_row=1, max_row=ws.max_row):
                for item in cell:
                    value = item.value
                    if value is None:
                        continue
                    text = str(value)
                    max_len = max(max_len, len(text))
            width = min(max(max_len + 2, 10), 38)
            if letter in {"P"} and ws.title == "Base_Pagos":
                width = 42
            ws.column_dimensions[letter].width = width

        for row in ws.iter_rows():
            for cell in row:
                header = str(ws.cell(row=1, column=cell.column).value or "").lower()
                if any(word in header for word in ["monto", "total", "generado", "pagado", "pendiente", "ganancia", "caja", "ecuasanitas", "precio", "saldo"]):
                    cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
                if "porcentaje" in header:
                    cell.number_format = "0%"
                if "fecha" in header:
                    cell.number_format = "dd/mm/yyyy"

        if ws.max_row > 1:
            for cell in ws[1]:
                if cell.value:
                    cell.fill = PatternFill("solid", fgColor=azul)
                    cell.font = Font(bold=True, color=blanco)

    # Estilo específico del dashboard.
    ws = wb["Dashboard"]
    ws.sheet_view.showGridLines = False
    for row in range(1, 40):
        ws.row_dimensions[row].height = 24
    for col in range(1, 11):
        ws.column_dimensions[chr(64 + col)].width = 18

    for rng in ["A1:J2", "A4:C10", "A13:C13", "D4:F6", "G4:I6", "D8:F10", "G8:I10", "D12:F14", "G12:I14"]:
        for row in ws[rng]:
            for cell in row:
                cell.fill = PatternFill("solid", fgColor=gris)
                cell.border = borde
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws["A1"].fill = PatternFill("solid", fgColor=azul)
    ws["A1"].font = Font(bold=True, color=blanco, size=16)
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")

    for cell_ref in ["A4", "D4", "G4", "D8", "G8", "D12", "G12", "A13"]:
        ws[cell_ref].font = Font(bold=True, color="12355B", size=11)
    for cell_ref in ["B5", "B6", "B7", "D5", "G5", "D9", "G9", "D13", "G13", "B13"]:
        ws[cell_ref].font = Font(bold=True, color="12355B", size=18 if cell_ref not in {"B5", "B6", "B7"} else 12)
        ws[cell_ref].fill = PatternFill("solid", fgColor=verde)
    ws["A4"].fill = PatternFill("solid", fgColor=azul)
    ws["A4"].font = Font(bold=True, color=blanco, size=12)


def _excel_agregar_tabla(ws, start_row: int, start_col: int, headers: List[str], rows: List[List], table_name: str, crear_tabla: bool = True) -> int:
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter

    header_row = start_row
    for idx, header in enumerate(headers, start=start_col):
        ws.cell(row=header_row, column=idx, value=header)

    data_rows = rows if rows else [["Sin datos"] + [None] * (len(headers) - 1)]
    for r_idx, row_values in enumerate(data_rows, start=header_row + 1):
        for c_idx, value in enumerate(row_values, start=start_col):
            ws.cell(row=r_idx, column=c_idx, value=value)

    end_row = header_row + len(data_rows)
    end_col = start_col + len(headers) - 1
    ref = f"{get_column_letter(start_col)}{header_row}:{get_column_letter(end_col)}{end_row}"
    if crear_tabla:
        tab = Table(displayName=table_name, ref=ref)
        tab.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        ws.add_table(tab)
        # IMPORTANTE:
        # No usar ws.auto_filter.ref cuando la hoja tiene tablas de Excel.
        # Cada Table ya incluye su propio autofiltro. En Microsoft Excel, combinar
        # filtros de hoja con tablas puede generar archivos .xlsx que se abren con
        # el mensaje: "Hemos encontrado un problema con contenido...".

    currency_words = [
        "monto",
        "total",
        "generado",
        "pagado",
        "pendiente",
        "ganancia",
        "caja",
        "ecuasanitas",
        "precio",
        "saldo",
    ]
    integer_words = ["sesiones", "duración", "dolor"]

    for offset, header in enumerate(headers):
        col_idx = start_col + offset
        header_lower = header.lower()
        for row_idx in range(header_row + 1, end_row + 1):
            cell = ws.cell(row=row_idx, column=col_idx)
            if "fecha" in header_lower:
                cell.number_format = "dd/mm/yyyy"
            elif any(word in header_lower for word in currency_words):
                cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
            elif any(word in header_lower for word in integer_words):
                cell.number_format = "0"

    return end_row + 3


def _excel_escribir_titulo(ws, titulo: str, subtitulo: str) -> None:
    ws.merge_cells("A1:J1")
    ws["A1"] = titulo
    ws.merge_cells("A2:J2")
    ws["A2"] = subtitulo


def _excel_colocar_kpi(ws, cell: str, titulo: str, valor, detalle: str) -> None:
    row = ws[cell].row
    col = ws[cell].column
    ws.cell(row=row, column=col, value=titulo)
    ws.cell(row=row + 1, column=col, value=valor)
    ws.cell(row=row + 2, column=col, value=detalle)
    ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + 2)
    ws.merge_cells(start_row=row + 1, start_column=col, end_row=row + 1, end_column=col + 2)
    ws.merge_cells(start_row=row + 2, start_column=col, end_row=row + 2, end_column=col + 2)


def _excel_unicos_con_todos(valores: List[str]) -> List[str]:
    limpios = sorted({str(v).strip() for v in valores if str(v or "").strip()})
    return ["Todos"] + limpios


def _excel_crear_listas_filtros(wb, base_sesiones: List[List], base_pagos: List[List]) -> Dict[str, str]:
    """Crea listas ocultas para los desplegables del panel central de filtros."""
    ws = wb.create_sheet("Listas")

    clinicas = _excel_unicos_con_todos(
        [row[6] for row in base_sesiones if len(row) > 6]
        + [row[6] for row in base_pagos if len(row) > 6]
    )
    fisios = _excel_unicos_con_todos(
        [row[5] for row in base_sesiones if len(row) > 5]
        + [row[5] for row in base_pagos if len(row) > 5]
    )
    # Semana CORPOFIT: domingo a sábado.
    dias = ["Todos", "Domingo", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado"]

    listas = {
        "clinicas": clinicas,
        "fisios": fisios,
        "dias": dias,
    }
    columnas = {
        "clinicas": 1,
        "fisios": 2,
        "dias": 3,
    }
    titulos = {
        "clinicas": "Clínicas",
        "fisios": "Fisioterapeutas",
        "dias": "Días",
    }

    rangos: Dict[str, str] = {}
    for nombre, valores in listas.items():
        col = columnas[nombre]
        ws.cell(row=1, column=col, value=titulos[nombre])
        for row_idx, value in enumerate(valores, start=2):
            ws.cell(row=row_idx, column=col, value=value)
        col_letter = chr(64 + col)
        rangos[nombre] = f"=Listas!${col_letter}$2:${col_letter}${len(valores) + 1}"

    ws.sheet_state = "hidden"
    return rangos


def _excel_formula_cond_sesiones(clinica_ref: str = "$B$5", fisio_ref: str = "$B$6", dia_ref: str = "$B$7") -> str:
    return (
        f"--ISNUMBER(tblBaseSesiones[ID Sesión]),"
        f"--((({clinica_ref}=\"Todos\")+(tblBaseSesiones[Clínica / Consultorio]={clinica_ref}))>0),"
        f"--((({fisio_ref}=\"Todos\")+(tblBaseSesiones[Fisioterapeuta]={fisio_ref}))>0),"
        f"--((({dia_ref}=\"Todos\")+(tblBaseSesiones[Día]={dia_ref}))>0)"
    )


def _excel_formula_cond_pagos(clinica_ref: str = "$B$5", fisio_ref: str = "$B$6", dia_ref: str = "$B$7") -> str:
    return (
        f"--ISNUMBER(tblBasePagos[ID Pago]),"
        f"--((({clinica_ref}=\"Todos\")+(tblBasePagos[Clínica / Consultorio]={clinica_ref}))>0),"
        f"--((({fisio_ref}=\"Todos\")+(tblBasePagos[Fisioterapeuta / Responsable]={fisio_ref}))>0),"
        f"--((({dia_ref}=\"Todos\")+(tblBasePagos[Día]={dia_ref}))>0)"
    )


def _excel_configurar_panel_filtros_dashboard(ws, rangos: Dict[str, str], desde: date, hasta: date) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill, Side, Border
    from openpyxl.worksheet.datavalidation import DataValidation

    azul = "1F4E78"
    azul_claro = "D9EAF7"
    verde = "E2F0D9"
    gris = "F4F6F8"
    blanco = "FFFFFF"
    borde = Border(
        left=Side(style="thin", color="B7C9D6"),
        right=Side(style="thin", color="B7C9D6"),
        top=Side(style="thin", color="B7C9D6"),
        bottom=Side(style="thin", color="B7C9D6"),
    )

    ws.merge_cells("A4:C4")
    ws["A4"] = "CAJITA DE FILTROS"
    ws["A4"].font = Font(bold=True, color=blanco, size=12)
    ws["A4"].fill = PatternFill("solid", fgColor=azul)
    ws["A4"].alignment = Alignment(horizontal="center", vertical="center")

    labels = [
        ("A5", "Clínica / Consultorio", "B5", "Todos", "clinicas"),
        ("A6", "Fisioterapeuta", "B6", "Todos", "fisios"),
        ("A7", "Día de semana", "B7", "Todos", "dias"),
    ]
    for label_cell, label, value_cell, default, rango_key in labels:
        ws[label_cell] = label
        ws[value_cell] = default
        ws[label_cell].font = Font(bold=True, color="12355B")
        ws[value_cell].fill = PatternFill("solid", fgColor=verde)
        ws[value_cell].font = Font(bold=True, color="12355B")
        ws[value_cell].alignment = Alignment(horizontal="center", vertical="center")
        dv = DataValidation(type="list", formula1=rangos[rango_key], allow_blank=False)
        dv.error = "Selecciona una opción válida de la lista."
        dv.errorTitle = "Filtro no válido"
        ws.add_data_validation(dv)
        dv.add(ws[value_cell])

    ws["A9"] = "Periodo"
    ws["B9"] = f"{desde.strftime('%d/%m/%Y')} - {hasta.strftime('%d/%m/%Y')}"
    ws["A10"] = "Uso"
    ws["B10"] = "Cambia los 3 desplegables y los valores se recalculan."
    ws["B10"].alignment = Alignment(wrap_text=True, vertical="center")

    for rng in ["A4:C10"]:
        for row in ws[rng]:
            for cell in row:
                cell.border = borde
                if cell.row >= 5 and cell.column != 2:
                    cell.fill = PatternFill("solid", fgColor=gris)

    filtros_sesiones = _excel_formula_cond_sesiones()
    filtros_pagos = _excel_formula_cond_pagos()

    formulas = {
        "sesiones": f"=SUMPRODUCT({filtros_sesiones})",
        "generado": f"=SUMPRODUCT(tblBaseSesiones[Precio sesión],{filtros_sesiones})",
        "pagado": f"=SUMPRODUCT(tblBasePagos[Caja válida],{filtros_pagos})",
        "ecuasanitas": f"=SUMPRODUCT(tblBaseSesiones[Precio sesión],--(tblBaseSesiones[Ecuasanitas]=\"Sí\"),{filtros_sesiones})",
        "ganancia_fisio": f"=SUMPRODUCT(tblBaseSesiones[Ganancia fisio],{filtros_sesiones})",
        "ganancia_clinica": f"=SUMPRODUCT(tblBaseSesiones[Ganancia clínica],{filtros_sesiones})",
    }
    formulas["pendiente"] = "=MAX(G5-D9-G9,0)"

    _excel_colocar_kpi(ws, "D4", "Sesiones filtradas", formulas["sesiones"], "Según clínica, fisio y día")
    _excel_colocar_kpi(ws, "G4", "Generado terapias", formulas["generado"], "Valor producido")
    _excel_colocar_kpi(ws, "D8", "Pagado caja", formulas["pagado"], "Cobros verificados")
    _excel_colocar_kpi(ws, "G8", "Ecuasanitas", formulas["ecuasanitas"], "Convenio terapias")
    _excel_colocar_kpi(ws, "D12", "Ganancia fisio", formulas["ganancia_fisio"], "35% Lun-Vie / 40% Sáb-Dom")
    _excel_colocar_kpi(ws, "G12", "Ganancia clínica", formulas["ganancia_clinica"], "65% Lun-Vie / 60% Sáb-Dom")

    ws["A13"] = "Pendiente estimado"
    ws["B13"] = formulas["pendiente"]
    ws["C13"] = "Generado - Pagado - Ecuasanitas"
    ws["A13"].font = Font(bold=True, color="12355B")
    ws["B13"].font = Font(bold=True, color="12355B", size=14)
    ws["B13"].fill = PatternFill("solid", fgColor=verde)
    ws["C13"].alignment = Alignment(wrap_text=True)

    for cell_ref in ["G5", "D9", "G9", "D13", "G13", "B13"]:
        ws[cell_ref].number_format = '$#,##0.00;[Red]-$#,##0.00'
    ws["D5"].number_format = "0"

    ws["A16"] = "Importante"
    ws["A16"].font = Font(bold=True, color="12355B", size=12)
    ws["A17"] = "• Esta hoja es el panel principal. No necesitas filtrar tabla por tabla."
    ws["A18"] = "• Las hojas Sesiones_Filtradas y Pagos_Filtrados usan estos mismos filtros."
    ws["A19"] = "• Las hojas Base_Sesiones y Base_Pagos quedan como respaldo completo de auditoría."
    ws["A20"] = "• Fisioterapeutas: lunes a viernes 35%; sábado y domingo 40%."
    ws["A21"] = "• Si cambias un filtro y no se actualiza, presiona F9 o guarda y vuelve a abrir el archivo."


def _excel_crear_vistas_filtradas(ws_sesiones, ws_pagos, sesiones_headers: List[str], pagos_headers: List[str]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    azul = "1F4E78"
    blanco = "FFFFFF"
    azul_claro = "D9EAF7"

    # Vista de sesiones filtrada por la cajita del Dashboard.
    _excel_escribir_titulo(
        ws_sesiones,
        "CORPOFIT PRO — SESIONES FILTRADAS",
        "Esta hoja se actualiza con la cajita de filtros del Dashboard."
    )
    ws_sesiones["A4"] = "Cambia Clínica, Fisioterapeuta o Día en Dashboard. Aquí se mostrará el detalle filtrado."
    ws_sesiones["A4"].fill = PatternFill("solid", fgColor=azul_claro)
    ws_sesiones["A4"].font = Font(bold=True, color="12355B")
    for idx, header in enumerate(sesiones_headers, start=1):
        cell = ws_sesiones.cell(row=6, column=idx, value=header)
        cell.fill = PatternFill("solid", fgColor=azul)
        cell.font = Font(bold=True, color=blanco)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    include_sesiones = (
        "ISNUMBER(tblBaseSesiones[ID Sesión])"
        "*(((Dashboard!$B$5=\"Todos\")+(tblBaseSesiones[Clínica / Consultorio]=Dashboard!$B$5))>0)"
        "*(((Dashboard!$B$6=\"Todos\")+(tblBaseSesiones[Fisioterapeuta]=Dashboard!$B$6))>0)"
        "*(((Dashboard!$B$7=\"Todos\")+(tblBaseSesiones[Día]=Dashboard!$B$7))>0)"
    )
    ws_sesiones["A7"] = f'=FILTER(tblBaseSesiones,{include_sesiones},"Sin resultados")'
    ws_sesiones.freeze_panes = "A7"

    # Vista de pagos filtrada por la cajita del Dashboard.
    _excel_escribir_titulo(
        ws_pagos,
        "CORPOFIT PRO — PAGOS FILTRADOS",
        "Esta hoja se actualiza con la cajita de filtros del Dashboard."
    )
    ws_pagos["A4"] = "Cambia Clínica, Fisioterapeuta o Día en Dashboard. Aquí se mostrará el detalle filtrado."
    ws_pagos["A4"].fill = PatternFill("solid", fgColor=azul_claro)
    ws_pagos["A4"].font = Font(bold=True, color="12355B")
    for idx, header in enumerate(pagos_headers, start=1):
        cell = ws_pagos.cell(row=6, column=idx, value=header)
        cell.fill = PatternFill("solid", fgColor=azul)
        cell.font = Font(bold=True, color=blanco)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    include_pagos = (
        "ISNUMBER(tblBasePagos[ID Pago])"
        "*(((Dashboard!$B$5=\"Todos\")+(tblBasePagos[Clínica / Consultorio]=Dashboard!$B$5))>0)"
        "*(((Dashboard!$B$6=\"Todos\")+(tblBasePagos[Fisioterapeuta / Responsable]=Dashboard!$B$6))>0)"
        "*(((Dashboard!$B$7=\"Todos\")+(tblBasePagos[Día]=Dashboard!$B$7))>0)"
    )
    ws_pagos["A7"] = f'=FILTER(tblBasePagos,{include_pagos},"Sin resultados")'
    ws_pagos.freeze_panes = "A7"


def _crear_excel_reporte_corpofit(
    db: Session,
    current_user: Usuario,
    desde: date,
    hasta: date,
    terapeutaid: Optional[int] = None,
    consultorioid: Optional[int] = None,
    dia_semana: Optional[int] = None,
) -> bytes:
    from openpyxl import Workbook
    from openpyxl.chart import BarChart, Reference, PieChart
    from openpyxl.styles import Alignment, Font, PatternFill

    general = reporte_terapias(
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
        db=db,
        current_user=current_user,
    )
    fisios = reporte_fisioterapeutas_semanal(
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
        db=db,
        current_user=current_user,
    )
    clinicas = reporte_clinicas_semanal(
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
        db=db,
        current_user=current_user,
    )

    base_sesiones = _excel_base_sesiones(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
    )
    base_pagos = _excel_base_pagos(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=terapeutaid,
        consultorioid=consultorioid,
        dia_semana=dia_semana,
    )

    wb = Workbook()
    # Forzar recálculo al abrir, porque el panel usa fórmulas vinculadas a tablas.
    try:
        wb.calculation.fullCalcOnLoad = True
        wb.calculation.forceFullCalc = True
        wb.calculation.calcMode = "auto"
    except Exception:
        pass

    ws_dashboard = wb.active
    ws_dashboard.title = "Dashboard"
    ws_sesiones_filtradas = wb.create_sheet("Sesiones_Filtradas")
    ws_pagos_filtrados = wb.create_sheet("Pagos_Filtrados")
    ws_analisis = wb.create_sheet("Análisis")
    ws_sesiones = wb.create_sheet("Base_Sesiones")
    ws_pagos = wb.create_sheet("Base_Pagos")

    subtitulo = (
        f"Periodo semanal completo: {desde.strftime('%d/%m/%Y')} - {hasta.strftime('%d/%m/%Y')} | "
        f"Generado: {now_ecuador().strftime('%d/%m/%Y %H:%M')}"
    )

    # Hoja Análisis
    _excel_escribir_titulo(ws_analisis, "CORPOFIT PRO — ANÁLISIS", subtitulo)
    current_row = 4
    ws_analisis.cell(row=current_row, column=1, value="▶ Resumen por día")
    current_row += 1
    daily_headers = [
        "Fecha",
        "Día",
        "Sesiones",
        "Generado terapias",
        "Pagado caja",
        "Ecuasanitas",
        "Pendiente estimado",
    ]
    daily_rows = []
    for item in general.sesiones_por_dia:
        excel_row = len(daily_rows) + current_row + 1
        daily_rows.append(
            [
                item.fecha,
                item.dia,
                item.sesiones,
                item.total_generado,
                item.pagos_verificados,
                item.cubierto_ecuasanitas,
                f"=MAX(D{excel_row}-E{excel_row}-F{excel_row},0)",
            ]
        )
    daily_table_start = current_row
    current_row = _excel_agregar_tabla(ws_analisis, current_row, 1, daily_headers, daily_rows, "tblAnalisisDias", crear_tabla=False)

    ws_analisis.cell(row=current_row, column=1, value="▶ Resumen por método de pago")
    current_row += 1
    metodo_headers = ["Método", "Total"]
    metodo_rows = [[m.metodo, m.total] for m in general.por_metodo_pago]
    metodo_table_start = current_row
    current_row = _excel_agregar_tabla(ws_analisis, current_row, 1, metodo_headers, metodo_rows, "tblAnalisisMetodos", crear_tabla=False)

    ws_analisis.cell(row=current_row, column=1, value="▶ Resumen por fisioterapeuta")
    current_row += 1
    fisio_headers = [
        "Terapeuta",
        "Clínica",
        "Sesiones",
        "Generado terapias",
        "Pagado pacientes",
        "Pendiente pacientes",
        "Ecuasanitas",
        "Gimnasio pagado",
        "Ganancia fisio total",
        "Ganancia fisio cobrada",
        "Ganancia fisio pendiente",
    ]
    fisio_rows = [
        [
            item.terapeuta,
            item.consultorio,
            item.sesiones_realizadas,
            item.total_generado,
            item.total_pagado_pacientes,
            item.total_pendiente_pacientes,
            item.total_ecuasanitas,
            item.total_gimnasio_pagado,
            item.ganancia_fisio_total,
            item.ganancia_fisio_cobrada,
            item.ganancia_fisio_pendiente,
        ]
        for item in fisios
    ]
    current_row = _excel_agregar_tabla(ws_analisis, current_row, 1, fisio_headers, fisio_rows, "tblAnalisisFisios", crear_tabla=False)

    ws_analisis.cell(row=current_row, column=1, value="▶ Resumen por clínica")
    current_row += 1
    clinica_headers = [
        "Clínica",
        "Sesiones",
        "Generado terapias",
        "Pagado pacientes",
        "Pendiente pacientes",
        "Ecuasanitas",
        "Gimnasio pagado",
        "Ganancia fisios total",
        "Ganancia clínica total",
        "Ganancia clínica cobrada",
        "Ganancia clínica pendiente",
    ]
    clinica_rows = [
        [
            item.consultorio,
            item.sesiones_realizadas,
            item.total_generado,
            item.total_pagado_pacientes,
            item.total_pendiente_pacientes,
            item.total_ecuasanitas,
            item.total_gimnasio_pagado,
            item.ganancia_fisios_total,
            item.ganancia_clinica_total,
            item.ganancia_clinica_cobrada,
            item.ganancia_clinica_pendiente,
        ]
        for item in clinicas
    ]
    current_row = _excel_agregar_tabla(ws_analisis, current_row, 1, clinica_headers, clinica_rows, "tblAnalisisClinicas", crear_tabla=False)

    # Base de sesiones
    sesiones_headers = [
        "ID Sesión",
        "Fecha",
        "Día",
        "Paciente",
        "Cédula",
        "Fisioterapeuta",
        "Clínica / Consultorio",
        "Clínica paciente",
        "Tratamiento",
        "Tratamiento ID",
        "Hora ingreso",
        "Hora salida",
        "Duración min",
        "Dolor entrada",
        "Dolor salida",
        "Precio sesión",
        "Ecuasanitas",
        "Porcentaje fisio",
        "Porcentaje clínica",
        "Ganancia fisio",
        "Ganancia clínica",
    ]
    _excel_agregar_tabla(ws_sesiones, 1, 1, sesiones_headers, base_sesiones, "tblBaseSesiones")
    ws_sesiones.freeze_panes = "A2"

    # Base de pagos
    pagos_headers = [
        "ID Pago",
        "Fecha Ecuador",
        "Día",
        "Paciente",
        "Cédula",
        "Fisioterapeuta / Responsable",
        "Clínica / Consultorio",
        "Tipo",
        "Método",
        "Estado",
        "Monto",
        "Pago previo",
        "Recuperación cartera",
        "Anulado",
        "Referencia",
        "Observación",
        "Cobrador ID",
        "Caja válida",
    ]
    _excel_agregar_tabla(ws_pagos, 1, 1, pagos_headers, base_pagos, "tblBasePagos")
    ws_pagos.freeze_panes = "A2"

    # Panel central de filtros: una sola cajita controla los resúmenes y vistas.
    rangos_filtros = _excel_crear_listas_filtros(wb, base_sesiones, base_pagos)
    _excel_escribir_titulo(ws_dashboard, "CORPOFIT PRO — REPORTE EXCEL", subtitulo)
    _excel_configurar_panel_filtros_dashboard(ws_dashboard, rangos_filtros, desde, hasta)
    _excel_crear_vistas_filtradas(ws_sesiones_filtradas, ws_pagos_filtrados, sesiones_headers, pagos_headers)

    # Gráficos del Dashboard basados en Análisis.
    if general.sesiones_por_dia:
        chart = BarChart()
        chart.title = "Generado vs pagado por día"
        chart.y_axis.title = "USD"
        chart.x_axis.title = "Día"
        data = Reference(
            ws_analisis,
            min_col=4,
            max_col=5,
            min_row=daily_table_start,
            max_row=daily_table_start + len(general.sesiones_por_dia),
        )
        cats = Reference(
            ws_analisis,
            min_col=2,
            min_row=daily_table_start + 1,
            max_row=daily_table_start + len(general.sesiones_por_dia),
        )
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(cats)
        chart.height = 8
        chart.width = 18
        ws_dashboard.add_chart(chart, "A21")

    if general.por_metodo_pago:
        pie = PieChart()
        pie.title = "Ingresos por método"
        data = Reference(
            ws_analisis,
            min_col=2,
            min_row=metodo_table_start,
            max_row=metodo_table_start + len(general.por_metodo_pago),
        )
        labels = Reference(
            ws_analisis,
            min_col=1,
            min_row=metodo_table_start + 1,
            max_row=metodo_table_start + len(general.por_metodo_pago),
        )
        pie.add_data(data, titles_from_data=True)
        pie.set_categories(labels)
        pie.height = 8
        pie.width = 11
        ws_dashboard.add_chart(pie, "G21")

    # Formatos y estilo visual.
    for ws in [ws_dashboard, ws_sesiones_filtradas, ws_pagos_filtrados, ws_analisis, ws_sesiones, ws_pagos]:
        ws.freeze_panes = ws.freeze_panes or "A4"
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = Alignment(vertical="center", wrap_text=False)

    _excel_aplicar_estilo_workbook(wb)

    for ws in [ws_sesiones_filtradas, ws_pagos_filtrados, ws_analisis, ws_sesiones, ws_pagos]:
        for row in ws.iter_rows(min_row=2):
            for cell in row:
                header = str(ws.cell(row=1, column=cell.column).value or "").lower()
                # En Análisis los encabezados no siempre están en fila 1; reforzamos por tipo de valor.
                if isinstance(cell.value, (int, float)) and any(
                    word in str(cell.offset(row=-(cell.row - 1)).value or "").lower()
                    for word in ["monto", "total", "generado", "pagado", "pendiente", "ganancia", "caja", "precio"]
                ):
                    cell.number_format = '$#,##0.00;[Red]-$#,##0.00'
                if isinstance(cell.value, date):
                    cell.number_format = "dd/mm/yyyy"

    # Ajuste final de formatos por columnas conocidas.
    for ws in [ws_dashboard, ws_sesiones_filtradas, ws_pagos_filtrados, ws_analisis, ws_sesiones, ws_pagos]:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, date):
                    cell.number_format = "dd/mm/yyyy"
                if isinstance(cell.value, (int, float)) and cell.column >= 4:
                    # La mayoría de valores monetarios están a partir de la columna D.
                    header_values = [str(ws.cell(row=r, column=cell.column).value or "").lower() for r in range(1, min(cell.row, 6) + 1)]
                    if any(
                        any(word in header for word in ["monto", "total", "generado", "pagado", "pendiente", "ganancia", "caja", "precio", "ecuasanitas"])
                        for header in header_values
                    ):
                        cell.number_format = '$#,##0.00;[Red]-$#,##0.00'

    output = BytesIO()
    wb.save(output)
    output.seek(0)
    return output.getvalue()


@router.get("/exportar-excel")
def exportar_excel_reportes(
    desde: Optional[date] = Query(None),
    hasta: Optional[date] = Query(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    desde, hasta = _default_range(desde, hasta)

    # El Excel siempre contiene toda la semana permitida para el rol.
    # Clínica/Consultorio, Fisioterapeuta y Día se controlan desde una sola
    # cajita de filtros en la hoja Dashboard.
    contenido = _crear_excel_reporte_corpofit(
        db=db,
        current_user=current_user,
        desde=desde,
        hasta=hasta,
        terapeutaid=None,
        consultorioid=None,
        dia_semana=None,
    )

    filename = f"corpofit_reporte_{desde.isoformat()}_{hasta.isoformat()}.xlsx"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    }

    return StreamingResponse(
        BytesIO(contenido),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
