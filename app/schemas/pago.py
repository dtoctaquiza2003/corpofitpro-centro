from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from app.utils.fechas import to_ecuador


class PagoCreate(BaseModel):
    pacienteid: int
    pacientepaqueteid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    membresiagimnasioid: Optional[int] = None

    monto: float = Field(..., gt=0)
    metodopago: str

    numerocomprobante: Optional[str] = None
    comprobanteurl: Optional[str] = None

    # 1 = Pendiente
    # 2 = Verificado
    # 3 = Rechazado
    estadopago: Optional[int] = 2

    # Fecha contable/de caja. Si se registra después de medianoche pero
    # pertenece al cierre anterior, la secretaria puede enviar esta fecha.
    fechapagoreal: Optional[date] = None

    model_config = ConfigDict(populate_by_name=True)


class PagoPrevioTratamientoCreate(BaseModel):
    """
    Registro de terapias pagadas antes de la implementación del sistema.

    Este registro reduce la deuda del tratamiento, pero no representa dinero
    recibido en caja en la fecha de registro.
    """

    pacienteid: int
    tratamientopacienteid: int
    monto: float = Field(..., gt=0)
    fechapagoreal: Optional[date] = None
    observacionpagoprevio: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)




class PagoPrevioGimnasioCreate(BaseModel):
    """
    Registro de membresías de gimnasio pagadas antes de usar el sistema.

    Reduce la deuda de la membresía, pero no representa dinero recibido
    en caja en la fecha de registro.
    """

    pacienteid: int
    membresiagimnasioid: int
    monto: float = Field(..., gt=0)
    fechapagoreal: Optional[date] = None
    observacionpagoprevio: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)


class RecuperacionCarteraCreate(BaseModel):
    """
    Dinero cobrado hoy por deuda/atenciones anteriores al inicio del sistema.

    Sí entra al cuadre de caja del día. No se asocia a tratamiento, paquete
    ni membresía, por lo tanto no reduce saldos ni genera comisiones
    automáticas de terapeuta.
    """

    pacienteid: int
    monto: float = Field(..., gt=0)
    metodopago: str
    fechapagoreal: Optional[date] = None
    observacion_cartera: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)




class PagoReasignarRequest(BaseModel):
    """
    Cambia el destino contable de un pago ya registrado.

    No anula ni duplica el pago: conserva monto, método, estado, comprobante,
    fechas y creador/verificador. Solo cambia paciente/destino para corregir
    pagos registrados en una terapia, membresía o paciente equivocado.
    """

    pacienteid: int = Field(..., ge=1)
    pacientepaqueteid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    membresiagimnasioid: Optional[int] = None
    motivo: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(populate_by_name=True)


class PagoOut(BaseModel):
    id: int
    pacienteid: int
    pacientepaqueteid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    membresiagimnasioid: Optional[int] = None

    monto: float
    metodopago: Optional[str] = None
    fechapago: datetime

    numerocomprobante: Optional[str] = None
    comprobanteurl: Optional[str] = None

    # 1 = Pendiente
    # 2 = Verificado
    # 3 = Rechazado
    estadopago: Optional[int] = 2

    creado_por_id: Optional[int] = None
    verificado_por_id: Optional[int] = None
    fecha_verificacion: Optional[datetime] = None
    motivo_rechazo: Optional[str] = None

    # Pago previo / saldo inicial
    espagoprevio: bool = False
    fechapagoreal: Optional[date] = None
    observacionpagoprevio: Optional[str] = None

    # Recuperación de cartera anterior cobrada hoy
    esrecuperacioncartera: bool = False
    observacion_cartera: Optional[str] = None

    # Anulación
    anulado: bool = False
    anulado_por_id: Optional[int] = None
    fecha_anulacion: Optional[datetime] = None
    motivo_anulacion: Optional[str] = None

    @field_serializer("fechapago", "fecha_verificacion", "fecha_anulacion")
    def serializar_fecha_ecuador(self, value: Optional[datetime], _info):
        return to_ecuador(value) if value is not None else None

    model_config = ConfigDict(from_attributes=True)


class PagoSimpleOut(BaseModel):
    id: int
    monto: float
    metodopago: Optional[str] = None
    fechapago: datetime

    membresiagimnasioid: Optional[int] = None

    numerocomprobante: Optional[str] = None
    comprobanteurl: Optional[str] = None

    # 1 = Pendiente
    # 2 = Verificado
    # 3 = Rechazado
    estadopago: Optional[int] = 2

    creado_por_id: Optional[int] = None
    verificado_por_id: Optional[int] = None
    fecha_verificacion: Optional[datetime] = None
    motivo_rechazo: Optional[str] = None

    # Pago previo / saldo inicial
    espagoprevio: bool = False
    fechapagoreal: Optional[date] = None
    observacionpagoprevio: Optional[str] = None

    # Recuperación de cartera anterior cobrada hoy
    esrecuperacioncartera: bool = False
    observacion_cartera: Optional[str] = None

    # Anulación
    anulado: bool = False
    anulado_por_id: Optional[int] = None
    fecha_anulacion: Optional[datetime] = None
    motivo_anulacion: Optional[str] = None

    @field_serializer("fechapago", "fecha_verificacion", "fecha_anulacion")
    def serializar_fecha_ecuador(self, value: Optional[datetime], _info):
        return to_ecuador(value) if value is not None else None

    model_config = ConfigDict(from_attributes=True)


class CuentaPaqueteOut(BaseModel):
    pacientepaqueteid: int
    pacienteid: int
    paciente: str
    paquete: str

    preciofinal: float
    pagado: float
    saldo: float
    estado_pago: str

    sesionescontratadas: int
    sesionesusadas: int
    sesionesdisponibles: int

    duraciondias: Optional[int] = None

    fechaasignacion: Optional[date] = None
    fechaexpiracion: Optional[date] = None

    pagos: List[PagoSimpleOut] = Field(default_factory=list)


class CuentaEcuasanitasOut(BaseModel):
    """
    Cuenta de terapias cubiertas por Ecuasanitas.

    No representa deuda del paciente ni ingreso de caja del día.
    Sirve para que la clínica vea cuánto debe cubrir/facturar el seguro
    por terapias recibidas. Gimnasio no se incluye aquí.
    """

    tratamientopacienteid: int
    pacienteid: int
    paciente: str

    terapeutaid: Optional[int] = None
    terapeuta: Optional[str] = None

    tratamiento: str
    tipoterapiaid: Optional[int] = None
    tipo_terapia: Optional[str] = None

    precio_sesion_aplicado: float = 0
    sesiones_cubiertas: int = 0
    total_cubierto: float = 0

    ganancia_terapeuta: float = 0
    valor_clinica: float = 0

    fecha_ultima_sesion: Optional[date] = None
    estado: str = "POR FACTURAR"

    model_config = ConfigDict(from_attributes=True)


class CuentaTratamientoOut(BaseModel):
    tratamientopacienteid: int
    pacienteid: int
    paciente: str

    # Opcional: cuando se filtra por el fisioterapeuta que realizó
    # la sesión, estos campos indican el fisio operativo usado.
    terapeuta_sesionid: Optional[int] = None
    terapeuta_sesion: Optional[str] = None

    tratamiento: str
    tipoterapiaid: Optional[int] = None
    tipo_terapia: Optional[str] = None

    precio_sesion_oficial: Optional[float] = None
    precio_sesion_aplicado: Optional[float] = None
    sesiones_estimadas: Optional[int] = None
    sesiones_realizadas: int

    total_generado: float
    # Total que reduce la deuda: pagos de caja + pagos previos/saldos iniciales.
    pagado_verificado: float
    # Dinero cobrado antes del sistema. No entra a caja ni ingresos del día.
    pago_previo_verificado: float = 0
    # Cortesías/canjes/exoneraciones cubren la deuda, pero no entran a caja.
    pagado_sin_caja_verificado: float = 0
    # Dinero realmente cobrado dentro del sistema. Útil para cuadre de caja.
    pagado_caja_verificado: float = 0
    pendiente_verificacion: float
    saldo: float
    saldo_favor: float = 0
    estado_pago: str

    motivo_precio_especial: Optional[str] = None

    fechainicio: date
    activo: bool

    pagos: List[PagoSimpleOut] = Field(default_factory=list)


class SesionPendienteTerapeutaOut(BaseModel):
    sesionid: int
    fecha: date
    horaingreso: Optional[str] = None
    valor_sesion: float = 0
    cubierto: float = 0
    pendiente: float = 0


class PacienteDeudaTerapeutaOut(BaseModel):
    pacienteid: int
    paciente: str
    cedula: Optional[str] = None
    tratamientopacienteid: int
    tratamiento: str
    tipo_terapia: Optional[str] = None
    terapeutaid: int
    terapeuta: str
    precio_sesion: float = 0
    sesiones_pendientes: int = 0
    total_deuda: float = 0
    fecha_ultima_pendiente: Optional[date] = None
    sesiones: List[SesionPendienteTerapeutaOut] = Field(default_factory=list)


class ResumenDeudasTerapeutaOut(BaseModel):
    terapeutaid: int
    terapeuta: str
    total_deuda: float = 0
    pacientes_con_deuda: int = 0
    tratamientos_con_deuda: int = 0
    sesiones_pendientes: int = 0
    items: List[PacienteDeudaTerapeutaOut] = Field(default_factory=list)


class CuentaMembresiaGimnasioOut(BaseModel):
    membresiagimnasioid: int
    pacienteid: int
    paciente: str

    fechainicio: date
    diascontratados: int
    precio: Optional[float] = None
    activo: bool
    observaciones: Optional[str] = None

    # Total que reduce la deuda: pagos de caja + pagos previos/saldos iniciales.
    pagado_verificado: float
    # Dinero cobrado antes del sistema. No entra a caja ni ingresos del día.
    pago_previo_verificado: float = 0
    # Cortesías/canjes/exoneraciones cubren la deuda, pero no entran a caja.
    pagado_sin_caja_verificado: float = 0
    # Dinero realmente cobrado dentro del sistema. Útil para cuadre de caja.
    pagado_caja_verificado: float = 0
    pendiente_verificacion: float
    saldo: float
    saldo_favor: float = 0
    estado_pago: str

    pagos: List[PagoSimpleOut] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True)


class PagoAnularRequest(BaseModel):
    motivo_anulacion: str = Field(..., min_length=5, max_length=500)