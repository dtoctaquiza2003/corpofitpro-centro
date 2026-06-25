from sqlalchemy import Column, Integer, Date, DateTime, ForeignKey
from sqlalchemy.sql import func
from sqlalchemy.orm import synonym

from ..database import Base


class Asistencia(Base):
    __tablename__ = "asistencias"

    id = Column(Integer, primary_key=True, index=True)
    pacienteid = Column("pacienteid", Integer, ForeignKey("pacientes.id"))
    fecha = Column("fecha", Date)
    horaregistro = Column(
        "horaregistro",
        DateTime(timezone=True),
        server_default=func.current_timestamp(),
    )

    paciente_id = synonym("pacienteid")
    hora_registro = synonym("horaregistro")