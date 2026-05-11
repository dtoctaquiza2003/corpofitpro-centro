from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class PagoCreate(BaseModel):
    pacienteid: int
    pacientepaqueteid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    monto: float = Field(..., gt=0)
    metodopago: str

    numerocomprobante: Optional[str] = None
    comprobanteurl: Optional[str] = None

    # 1 = Pendiente
    # 2 = Verificado
    # 3 = Rechazado
    estadopago: Optional[int] = 2

    model_config = ConfigDict(populate_by_name=True)


class PagoOut(BaseModel):
    id: int
    pacienteid: int
    pacientepaqueteid: Optional[int] = None
    tratamientopacienteid: Optional[int] = None
    monto: float
    metodopago: Optional[str] = None
    fechapago: datetime

    numerocomprobante: Optional[str] = None
    comprobanteurl: Optional[str] = None
    estadopago: Optional[int] = 2

    creado_por_id: Optional[int] = None
    verificado_por_id: Optional[int] = None
    fecha_verificacion: Optional[datetime] = None
    motivo_rechazo: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    


class PagoSimpleOut(BaseModel):
    id: int
    monto: float
    metodopago: Optional[str] = None
    fechapago: datetime

    numerocomprobante: Optional[str] = None
    comprobanteurl: Optional[str] = None
    estadopago: Optional[int] = 2

    creado_por_id: Optional[int] = None
    verificado_por_id: Optional[int] = None
    fecha_verificacion: Optional[datetime] = None
    motivo_rechazo: Optional[str] = None

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


class CuentaTratamientoOut(BaseModel):
    tratamientopacienteid: int
    pacienteid: int
    paciente: str

    tratamiento: str
    tipoterapiaid: Optional[int] = None
    tipo_terapia: Optional[str] = None

    precio_sesion_oficial: Optional[float] = None
    precio_sesion_aplicado: Optional[float] = None
    sesiones_estimadas: Optional[int] = None
    sesiones_realizadas: int

    total_generado: float
    pagado_verificado: float
    pendiente_verificacion: float
    saldo: float
    estado_pago: str

    motivo_precio_especial: Optional[str] = None

    fechainicio: date
    activo: bool

    pagos: List[PagoSimpleOut] = Field(default_factory=list)