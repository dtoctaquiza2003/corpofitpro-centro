from sqlalchemy import (
    Column,
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

    # Nuevos campos para auditoría y notificaciones
    creado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    verificado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    fecha_verificacion = Column(DateTime(timezone=True), nullable=True)
    motivo_rechazo = Column(Text, nullable=True)