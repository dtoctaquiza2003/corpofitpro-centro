from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.sql import func

from ..database import Base
from ..utils.fechas import now_utc


class Egreso(Base):
    __tablename__ = "egresos"

    id = Column(Integer, primary_key=True, index=True)

    consultorioid = Column(Integer, ForeignKey("consultorios.id"), nullable=False, index=True)
    fechaegreso = Column(Date, nullable=False, index=True)

    categoria = Column(String(80), nullable=False, default="General")
    concepto = Column(String(200), nullable=False)
    monto = Column(Float, nullable=False)
    metodopago = Column(String(50), nullable=False, default="Efectivo")
    observacion = Column(Text, nullable=True)

    creado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    fechacreacion = Column(
        DateTime(timezone=True),
        default=now_utc,
        server_default=func.now(),
        nullable=False,
    )

    anulado = Column(Boolean, default=False, nullable=False)
    motivo_anulacion = Column(Text, nullable=True)
    fecha_anulacion = Column(DateTime(timezone=True), nullable=True)
    anulado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
