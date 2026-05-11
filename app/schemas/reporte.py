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
    pendiente_cobro: float = 0
    saldo_a_favor: float = 0
    pendiente_verificacion: float = 0


class ReporteDiaOut(BaseModel):
    fecha: date
    dia: str
    sesiones: int = 0
    total_generado: float = 0
    pagos_verificados: float = 0


class TerapiasReporteOut(BaseModel):
    desde: date
    hasta: date
    total_sesiones: int = 0
    total_generado: float = 0
    total_pagado_verificado: float = 0
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
    total_generado: float = 0
    total_pagado_pacientes: float = 0
    total_pendiente_pacientes: float = 0
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
    total_generado: float = 0
    total_pagado_pacientes: float = 0
    total_pendiente_pacientes: float = 0
    ganancia_fisios_total: float = 0
    ganancia_fisios_cobrada: float = 0
    ganancia_fisios_pendiente: float = 0
    ganancia_clinica_total: float = 0
    ganancia_clinica_cobrada: float = 0
    ganancia_clinica_pendiente: float = 0
