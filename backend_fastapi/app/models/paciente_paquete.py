from sqlalchemy import Column, Integer, Date, String, ForeignKey, Numeric
from sqlalchemy.orm import synonym

from ..database import Base


class PacientePaquete(Base):
    __tablename__ = "pacientepaquete"

    id = Column(Integer, primary_key=True, index=True)

    pacienteid = Column("pacienteid", Integer, ForeignKey("pacientes.id"))
    paqueteid = Column("paqueteid", Integer, ForeignKey("paquetes.id"))
    preciofinal = Column("preciofinal", Numeric(10, 2), nullable=False)
    sesionescontratadas = Column("sesionescontratadas", Integer)
    sesionesusadas = Column("sesionesusadas", Integer, default=0)
    fechaasignacion = Column("fechaasignacion", Date)
    fechaexpiracion = Column("fechaexpiracion", Date)
    estado = Column("estado", String(20), default="ACTIVO")

    paciente_id = synonym("pacienteid")
    paquete_id = synonym("paqueteid")
    precio_final = synonym("preciofinal")
    sesiones_contratadas = synonym("sesionescontratadas")
    sesiones_usadas = synonym("sesionesusadas")
    fecha_asignacion = synonym("fechaasignacion")
    fecha_expiracion = synonym("fechaexpiracion")