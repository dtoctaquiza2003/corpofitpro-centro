from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.sql import func

from ..database import Base


class Pago(Base):
    __tablename__ = "pagos"

    id = Column(Integer, primary_key=True, index=True)

    pacienteid = Column(Integer, ForeignKey("pacientes.id"), nullable=False)
    pacientepaqueteid = Column(Integer, ForeignKey("pacientepaquete.id"), nullable=True)
    tratamientopacienteid = Column(
        Integer,
        ForeignKey("tratamientos_paciente.id"),
        nullable=True,
    )

    membresiagimnasioid = Column(
        Integer,
        ForeignKey("membresias_gimnasio.id"),
        nullable=True,
    )

    monto = Column(Float, nullable=False)
    metodopago = Column(String(50), nullable=False)

    fechapago = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )

    numerocomprobante = Column(String(100), nullable=True)
    comprobanteurl = Column(Text, nullable=True)

    # 1 = Pendiente
    # 2 = Verificado
    # 3 = Rechazado
    estadopago = Column(SmallInteger, default=2, nullable=False)

    # Auditoría de creación / verificación
    creado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    verificado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    fecha_verificacion = Column(DateTime(timezone=True), nullable=True)
    motivo_rechazo = Column(Text, nullable=True)


    # Pago previo / saldo inicial: dinero cobrado antes de usar el sistema.
    # Cuenta para reducir la deuda del tratamiento, pero NO debe entrar a caja
    # ni a ingresos cobrados del día porque no se cobró dentro del sistema.
    espagoprevio = Column(Boolean, default=False, nullable=False)
    fechapagoreal = Column(Date, nullable=True)
    observacionpagoprevio = Column(Text, nullable=True)

    # Anulación de pagos
    anulado = Column(Boolean, default=False, nullable=False)
    anulado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    fecha_anulacion = Column(DateTime(timezone=True), nullable=True)
    motivo_anulacion = Column(Text, nullable=True)