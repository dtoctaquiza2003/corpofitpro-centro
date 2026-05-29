from datetime import date
from typing import List, Optional

from pydantic import BaseModel, Field


# -----------------------------------------------------------------------------
# Reporte semanal antiguo
# -----------------------------------------------------------------------------

class SesionPorDia(BaseModel):
    dia: str
    fecha: date
    cantidad: int


class DetalleSesionReporte(BaseModel):
    id: int
    fecha: date
    paciente: str | None = None
    terapeuta: str | None = None


class ReporteSemanalResponse(BaseModel):
    fecha_inicio: date
    fecha_fin: date
    sesiones_por_dia: List[SesionPorDia]
    total_sesiones: int
    detalle: List[DetalleSesionReporte]


# -----------------------------------------------------------------------------
# Filtros
# -----------------------------------------------------------------------------

class ReporteFiltroTerapeutaOut(BaseModel):
    id: int
    nombre: str
    consultorioid: Optional[int] = None


class ReporteFiltroConsultorioOut(BaseModel):
    id: int
    nombre: str


class ReporteFiltrosOut(BaseModel):
    terapeutas: List[ReporteFiltroTerapeutaOut] = Field(default_factory=list)
    consultorios: List[ReporteFiltroConsultorioOut] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Dashboard
# -----------------------------------------------------------------------------

class DashboardResumenOut(BaseModel):
    sesiones_hoy: int = 0
    pacientes_atendidos_hoy: int = 0
    tratamientos_activos: int = 0
    ingresos_hoy: float = 0
    cuentas_pendientes: int = 0
    saldo_pendiente_total: float = 0
    transferencias_pendientes: int = 0
    saldo_a_favor_total: float = 0

class DashboardLiteOut(DashboardResumenOut):
    alertas_no_leidas: int = 0
    notificaciones_no_leidas: int = 0
    cesiones_activas: int = 0


class DashboardAccionesOut(BaseModel):
    """
    Resumen liviano para las tarjetas accionables del dashboard.

    Importante: este esquema NO incluye cálculos de cuentas/saldos, porque esos
    reportes son más pesados y deben cargarse solo cuando el usuario entra a
    Pagos o Reportes.
    """

    sesiones_hoy: int = 0
    sesiones_en_curso: int = 0
    sesiones_finalizadas_hoy: int = 0

    pacientes_activos: int = 0
    pacientes_nuevos_semana: int = 0

    tratamientos_activos: int = 0
    tratamientos_sin_sesion_7_dias: int = 0

    transferencias_pendientes: int = 0
    alertas_no_leidas: int = 0
    notificaciones_no_leidas: int = 0
    cesiones_activas: int = 0


# -----------------------------------------------------------------------------
# Reporte general de terapias
# -----------------------------------------------------------------------------

class MetodoPagoTotalOut(BaseModel):
    metodo: str
    total: float


class TratamientoRealizadoOut(BaseModel):
    tratamiento: str
    sesiones: int
    total_generado: float


class ResumenEstadoPagosOut(BaseModel):
    pagado_verificado: float = 0
    # Dinero pagado antes de usar CORPOFIT Pro. Reduce saldos, pero no es caja actual.
    pago_previo: float = 0
    pendiente_cobro: float = 0
    saldo_a_favor: float = 0
    pendiente_verificacion: float = 0
    cubierto_ecuasanitas: float = 0


class ReporteDiaOut(BaseModel):
    fecha: date
    dia: str
    sesiones: int = 0
    total_generado: float = 0
    pagos_verificados: float = 0
    cubierto_ecuasanitas: float = 0


class TerapiasReporteOut(BaseModel):
    desde: date
    hasta: date
    total_sesiones: int = 0
    total_generado: float = 0
    total_pagado_verificado: float = 0
    # Pago previo / saldo inicial: no entra al cuadre de caja del rango.
    total_pago_previo_verificado: float = 0
    total_ecuasanitas: float = 0
    sesiones_ecuasanitas: int = 0
    total_pendiente: float = 0
    saldo_a_favor: float = 0
    transferencias_pendientes: int = 0
    pendiente_verificacion_total: float = 0
    por_metodo_pago: List[MetodoPagoTotalOut] = Field(default_factory=list)
    tratamientos_mas_realizados: List[TratamientoRealizadoOut] = Field(default_factory=list)
    sesiones_por_dia: List[ReporteDiaOut] = Field(default_factory=list)
    estado_pagos: ResumenEstadoPagosOut = Field(default_factory=ResumenEstadoPagosOut)


# -----------------------------------------------------------------------------
# Reporte de fisioterapeutas
# -----------------------------------------------------------------------------

class FisioSemanalOut(BaseModel):
    terapeutaid: int
    terapeuta: str
    consultorioid: Optional[int] = None
    consultorio: str = "Sin consultorio"
    sesiones_realizadas: int = 0
    # Terapias: valores de sesiones finalizadas.
    total_generado: float = 0
    total_pagado_pacientes: float = 0
    total_pendiente_pacientes: float = 0
    total_ecuasanitas: float = 0

    # Gimnasio: pagos verificados de membresía mensual y pase diario.
    total_gimnasio_pagado: float = 0

    # Desglose de ganancia del terapeuta.
    ganancia_terapia_total: float = 0
    ganancia_terapia_cobrada: float = 0
    ganancia_terapia_pendiente: float = 0
    ganancia_terapia_ecuasanitas: float = 0
    ganancia_gimnasio_cobrada: float = 0

    # Totales finales para compatibilidad con el frontend existente.
    ganancia_fisio_total: float = 0
    ganancia_fisio_cobrada: float = 0
    ganancia_fisio_pendiente: float = 0


class FisioDetallePacienteOut(BaseModel):
    pacienteid: int
    paciente: str
    tratamientopacienteid: int
    tratamiento: str
    consultorioid: Optional[int] = None
    consultorio: str = "Sin consultorio"
    sesiones: int = 0
    precio_sesion: float = 0
    total_generado: float = 0
    pagado_paciente: float = 0
    pendiente_paciente: float = 0
    es_ecuasanitas: bool = False
    cubierto_ecuasanitas: float = 0
    ganancia_fisio: float = 0
    ganancia_cobrada: float = 0
    ganancia_pendiente: float = 0


class FisioDetalleOut(BaseModel):
    terapeutaid: int
    terapeuta: str
    desde: date
    hasta: date
    pacientes: List[FisioDetallePacienteOut] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Reporte de clínicas / consultorios
# -----------------------------------------------------------------------------

class ClinicaSemanalOut(BaseModel):
    consultorioid: Optional[int] = None
    consultorio: str = "Sin consultorio"
    sesiones_realizadas: int = 0
    # Terapias: valores de sesiones finalizadas.
    total_generado: float = 0
    total_pagado_pacientes: float = 0
    total_pendiente_pacientes: float = 0
    total_ecuasanitas: float = 0

    # Gimnasio: pagos verificados de membresía mensual y pase diario.
    total_gimnasio_pagado: float = 0

    # Desglose de fisioterapeutas.
    ganancia_fisios_terapia_total: float = 0
    ganancia_fisios_terapia_cobrada: float = 0
    ganancia_fisios_terapia_pendiente: float = 0
    ganancia_fisios_terapia_ecuasanitas: float = 0
    ganancia_fisios_gimnasio_cobrada: float = 0

    # Desglose de clínica.
    ganancia_clinica_terapia_total: float = 0
    ganancia_clinica_terapia_cobrada: float = 0
    ganancia_clinica_terapia_pendiente: float = 0
    ganancia_clinica_terapia_ecuasanitas: float = 0
    ganancia_clinica_gimnasio_cobrada: float = 0

    # Totales finales para compatibilidad con el frontend existente.
    ganancia_fisios_total: float = 0
    ganancia_fisios_cobrada: float = 0
    ganancia_fisios_pendiente: float = 0
    ganancia_clinica_total: float = 0
    ganancia_clinica_cobrada: float = 0
    ganancia_clinica_pendiente: float = 0
