from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text, func
from ..database import Base


class MembresiaGimnasio(Base):
    __tablename__ = "membresias_gimnasio"

    id = Column(Integer, primary_key=True, index=True)
    pacienteid = Column(Integer, ForeignKey("pacientes.id", ondelete="CASCADE"), nullable=False)

    fechainicio = Column(Date, nullable=False)
    diascontratados = Column(SmallInteger, nullable=False, default=20)
    precio = Column(Numeric(10, 2), nullable=True)

    modalidad = Column(String(20), nullable=False, default="MENSUAL")

    activo = Column(Boolean, nullable=False, default=True)
    observaciones = Column(Text, nullable=True)
    fechacreacion = Column(DateTime, server_default=func.now())


class MovimientoGimnasio(Base):
    __tablename__ = "movimientos_gimnasio"

    id = Column(Integer, primary_key=True, index=True)

    membresiaid = Column(
        Integer,
        ForeignKey("membresias_gimnasio.id", ondelete="CASCADE"),
        nullable=False,
    )

    pacienteid = Column(
        Integer,
        ForeignKey("pacientes.id", ondelete="CASCADE"),
        nullable=False,
    )

    fecha = Column(Date, nullable=False)

    # 1 = asistió a gimnasio
    # 2 = terapia reemplazó gimnasio
    tipo = Column(SmallInteger, nullable=False)

    sesionid = Column(
        Integer,
        ForeignKey("sesionesterapia.id", ondelete="SET NULL"),
        nullable=True,
    )

    tratamientopacienteid = Column(
        Integer,
        ForeignKey("tratamientos_paciente.id", ondelete="SET NULL"),
        nullable=True,
    )

    observacion = Column(Text, nullable=True)
    fechacreacion = Column(DateTime, server_default=func.now())