from sqlalchemy import Boolean, Column, Integer, Date, Time, SmallInteger, String, ForeignKey, FetchedValue
from sqlalchemy.orm import relationship, synonym

from ..database import Base


class SesionTerapia(Base):
    __tablename__ = "sesionesterapia"

    id = Column(Integer, primary_key=True, index=True)

    pacienteid = Column("pacienteid", Integer, ForeignKey("pacientes.id"))
    terapeutaid = Column("terapeutaid", Integer, ForeignKey("usuarios.id"))
    fecha = Column("fecha", Date)

    horaingreso = Column("horaingreso", Time)
    horasalida = Column("horasalida", Time, nullable=True)

    escaladolorentrada = Column("escaladolorentrada", SmallInteger, default=0)
    escaladolorsalida = Column("escaladolorsalida", SmallInteger, default=0)

    # Sistema antiguo de paquetes
    pacientepaqueteid = Column(
        "pacientepaqueteid",
        Integer,
        ForeignKey("pacientepaquete.id"),
        nullable=True,
    )

    # Nuevo sistema por tratamiento
    tratamientopacienteid = Column(
        "tratamientopacienteid",
        Integer,
        ForeignKey("tratamientos_paciente.id"),
        nullable=True,
    )

    # Columna generada por PostgreSQL.
    # No se debe insertar ni actualizar manualmente.
    duracionminutos = Column(
        "duracionminutos",
        Integer,
        server_default=FetchedValue(),
        server_onupdate=FetchedValue(),
    )

    # Análisis clínico liviano: se marca solo cuando las últimas 3 sesiones
    # relativamente seguidas no muestran reducción del dolor.
    analisisdolorrequerido = Column(
        "analisisdolorrequerido",
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    motivodolornodisminuye = Column(
        "motivodolornodisminuye",
        String(600),
        nullable=True,
    )
    dolorreferenciaprogreso = Column(
        "dolorreferenciaprogreso",
        SmallInteger,
        nullable=True,
    )
    doloractualprogreso = Column(
        "doloractualprogreso",
        SmallInteger,
        nullable=True,
    )

    paciente = relationship("Paciente")
    terapeuta = relationship("Usuario")
    tratamiento_paciente = relationship("TratamientoPaciente")

    # Alias para compatibilidad con código viejo
    paciente_id = synonym("pacienteid")
    terapeuta_id = synonym("terapeutaid")
    hora_ingreso = synonym("horaingreso")
    hora_salida = synonym("horasalida")
    escala_dolor_entrada = synonym("escaladolorentrada")
    escala_dolor_salida = synonym("escaladolorsalida")
    paciente_paquete_id = synonym("pacientepaqueteid")
    tratamiento_paciente_id = synonym("tratamientopacienteid")
    duracion_minutos = synonym("duracionminutos")
    analisis_dolor_requerido = synonym("analisisdolorrequerido")
    motivo_dolor_no_disminuye = synonym("motivodolornodisminuye")
    dolor_referencia_progreso = synonym("dolorreferenciaprogreso")
    dolor_actual_progreso = synonym("doloractualprogreso")