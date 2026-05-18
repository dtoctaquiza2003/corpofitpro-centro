from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from ..services.notificacion_service import crear_notificacion_usuario
from ..auth.dependencies import get_current_terapeuta, get_current_user
from ..auth.permissions import validar_consultorio_secretario
from ..dependencies.db import get_db
from ..models.alerta import Alerta
from ..models.asistencia import Asistencia
from ..models.paciente import Paciente
from ..models.sesion_terapia import SesionTerapia
from ..models.tratamiento import SesionTratamiento, TipoTratamiento
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..schemas.sesion import (
    FinalizarSesionCreate,
    InicioSesionCreate,
    SesionAtencionOut,
    TipoTratamientoOut,
)
from ..models.paciente_terapeuta_compartido import PacienteTerapeutaCompartido
from ..models.transferencia import Transferencia


router = APIRouter(prefix="/api/sesiones", tags=["sesiones"])


def now_ecuador() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))


def _tiene_autorizacion_compartida_activa(
    db: Session,
    paciente_id: int,
    terapeuta_id: int,
) -> bool:
    hoy = now_ecuador().date()

    existe = (
        db.query(PacienteTerapeutaCompartido.id)
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente_id,
            PacienteTerapeutaCompartido.terapeutaid == terapeuta_id,
            PacienteTerapeutaCompartido.activo == True,
            or_(
                PacienteTerapeutaCompartido.fecha_inicio == None,
                PacienteTerapeutaCompartido.fecha_inicio <= hoy,
            ),
            or_(
                PacienteTerapeutaCompartido.fecha_fin == None,
                PacienteTerapeutaCompartido.fecha_fin >= hoy,
            ),
        )
        .first()
    )

    return existe is not None


def _tiene_cesion_temporal_activa(
    db: Session,
    paciente_id: int,
    terapeuta_id: int,
) -> bool:
    transferencias = (
        db.query(Transferencia)
        .filter(
            Transferencia.terapeuta_destino_id == terapeuta_id,
            Transferencia.activo == True,
        )
        .options(joinedload(Transferencia.pacientes))
        .all()
    )

    for transferencia in transferencias:
        for paciente in transferencia.pacientes:
            if paciente.id == paciente_id:
                return True

    return False


def _terapeuta_puede_atender_paciente(
    db: Session,
    paciente: Paciente,
    current_user: Usuario,
) -> bool:
    if current_user.rol != 2:
        return False

    # 1. Paciente propio
    if paciente.terapeutaasignadoid == current_user.id:
        return True

    # 2. Paciente compartido excepcionalmente
    if _tiene_autorizacion_compartida_activa(
        db=db,
        paciente_id=paciente.id,
        terapeuta_id=current_user.id,
    ):
        return True

    # 3. Paciente cedido temporalmente
    if _tiene_cesion_temporal_activa(
        db=db,
        paciente_id=paciente.id,
        terapeuta_id=current_user.id,
    ):
        return True

    return False


def build_sesion_out(
    sesion: SesionTerapia,
    db: Session,
) -> SesionAtencionOut:
    paciente_nombre = None
    tratamiento_nombre = None
    precio_sesion_aplicado = None
    tratamientos_aplicados: list[str] = []

    if sesion.paciente:
        paciente_nombre = f"{sesion.paciente.nombres} {sesion.paciente.apellidos}"

    if sesion.tratamiento_paciente:
        tratamiento_nombre = sesion.tratamiento_paciente.tipotratamiento

        if sesion.tratamiento_paciente.precio_sesion_aplicado is not None:
            precio_sesion_aplicado = float(
                sesion.tratamiento_paciente.precio_sesion_aplicado
            )

    if sesion.id:
        tratamientos_db = (
            db.query(TipoTratamiento.nombre)
            .join(
                SesionTratamiento,
                SesionTratamiento.tratamientoid == TipoTratamiento.id,
            )
            .filter(SesionTratamiento.sesionid == sesion.id)
            .order_by(TipoTratamiento.nombre.asc())
            .all()
        )

        tratamientos_aplicados = [
            item.nombre for item in tratamientos_db if item.nombre
        ]

    return SesionAtencionOut(
        id=sesion.id,
        pacienteid=sesion.pacienteid,
        terapeutaid=sesion.terapeutaid,
        paciente=paciente_nombre,
        fecha=sesion.fecha,
        horaingreso=sesion.horaingreso,
        horasalida=sesion.horasalida,
        duracionminutos=sesion.duracionminutos,
        escaladolorentrada=sesion.escaladolorentrada,
        escaladolorsalida=sesion.escaladolorsalida if sesion.horasalida else None,
        pacientepaqueteid=sesion.pacientepaqueteid,
        tratamientopacienteid=sesion.tratamientopacienteid,
        tratamiento=tratamiento_nombre,
        precio_sesion_aplicado=precio_sesion_aplicado,
        estado="FINALIZADA" if sesion.horasalida else "EN_CURSO",
        tratamientos=tratamientos_aplicados,
    )


def obtener_tratamiento_activo(
    db: Session,
    paciente_id: int,
    tratamiento_id: Optional[int],
) -> TratamientoPaciente:
    query = db.query(TratamientoPaciente).filter(
        TratamientoPaciente.pacienteid == paciente_id,
        TratamientoPaciente.activo == True,
    )

    if tratamiento_id is not None:
        tratamiento = query.filter(TratamientoPaciente.id == tratamiento_id).first()

        if not tratamiento:
            raise HTTPException(
                status_code=400,
                detail="El tratamiento seleccionado no existe o no está activo.",
            )

        return tratamiento

    tratamientos = query.order_by(TratamientoPaciente.fechainicio.desc()).all()

    if not tratamientos:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este paciente no tiene un tratamiento activo. "
                "Debe crear un tratamiento antes de iniciar la atención."
            ),
        )

    if len(tratamientos) > 1:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este paciente tiene varios tratamientos activos. "
                "Seleccione el tratamiento antes de iniciar la sesión."
            ),
        )

    return tratamientos[0]

def validar_sesiones_disponibles_para_tratamiento(
    db: Session,
    tratamiento: TratamientoPaciente,
) -> None:
    """
    Evita iniciar nuevas atenciones cuando el tratamiento ya llegó al número
    de sesiones estimadas. Esta validación queda en backend para que el front
    no tenga que cargar todas las sesiones generales solo para calcularlo.
    """
    if tratamiento.sesiones_estimadas is None or tratamiento.sesiones_estimadas <= 0:
        return

    sesiones_realizadas = (
        db.query(SesionTerapia.id)
        .filter(
            SesionTerapia.pacienteid == tratamiento.pacienteid,
            SesionTerapia.tratamientopacienteid == tratamiento.id,
            SesionTerapia.horasalida != None,
        )
        .count()
    )

    if sesiones_realizadas >= tratamiento.sesiones_estimadas:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este tratamiento ya no tiene sesiones disponibles. "
                "No se puede iniciar una nueva atención."
            ),
        )


def _nombre_paciente(paciente: Paciente | None) -> str:
    if not paciente:
        return "Paciente"

    return f"{paciente.nombres} {paciente.apellidos}".strip()


def _obtener_usuarios_para_alerta(
    db: Session,
    paciente: Paciente,
) -> list[Usuario]:
    usuarios: list[Usuario] = []
    usuarios_ids = set()

    # 1. Terapeuta asignado del paciente
    if paciente.terapeutaasignadoid:
        terapeuta = (
            db.query(Usuario)
            .filter(
                Usuario.id == paciente.terapeutaasignadoid,
                Usuario.activo == True,
            )
            .first()
        )

        if terapeuta and terapeuta.id not in usuarios_ids:
            usuarios.append(terapeuta)
            usuarios_ids.add(terapeuta.id)

    # 2. Secretarios del consultorio del paciente
    secretarios = (
        db.query(Usuario)
        .filter(
            Usuario.rol == 1,
            Usuario.activo == True,
            Usuario.consultorioid == paciente.consultorioid,
        )
        .all()
    )

    for secretario in secretarios:
        if secretario.id not in usuarios_ids:
            usuarios.append(secretario)
            usuarios_ids.add(secretario.id)

    # 3. Jefes
    jefes = (
        db.query(Usuario)
        .filter(
            Usuario.rol == 3,
            Usuario.activo == True,
        )
        .all()
    )

    for jefe in jefes:
        if jefe.id not in usuarios_ids:
            usuarios.append(jefe)
            usuarios_ids.add(jefe.id)

    return usuarios

def _notificar_alerta_clinica(
    db: Session,
    alerta: Alerta,
    paciente: Paciente,
    sesion: SesionTerapia,
    current_user: Usuario,
) -> None:
    nombre_paciente = _nombre_paciente(paciente)

    if alerta.tipo == "high_pain":
        titulo = "Alerta clínica: dolor crítico"
        mensaje = f"{nombre_paciente} registró dolor crítico."
    elif alerta.tipo == "pain_increase":
        titulo = "Alerta clínica: aumento de dolor"
        mensaje = f"{nombre_paciente} registró un aumento de dolor."
    else:
        titulo = "Alerta clínica"
        mensaje = f"{nombre_paciente} generó una alerta clínica."

    usuarios_destino = _obtener_usuarios_para_alerta(
        db=db,
        paciente=paciente,
    )

    if not usuarios_destino:
        print("⚠️ No se encontraron usuarios destino para la alerta clínica.")
        return

    for usuario in usuarios_destino:
        if usuario.id == current_user.id:
            continue
        crear_notificacion_usuario(
            db=db,
            usuarioid=usuario.id,
            titulo=titulo,
            mensaje=mensaje,
            tipo="alerta_clinica",
            referencia_tipo="alerta",
            referencia_id=alerta.id,
            data={
                "alerta_id": alerta.id,
                "paciente_id": paciente.id,
                "sesion_id": sesion.id,
                "consultorioid": paciente.consultorioid,
                "tipo_alerta": alerta.tipo,
                "descripcion": alerta.descripcion,
                "terapeuta_id": sesion.terapeutaid,
                "creado_por_id": current_user.id,
                "actualizar": [
                    "alertas",
                    "dashboard",
                    "notificaciones",
                ],
            },
            hacer_flush=False,
        )

    db.flush()

    print(
        f"✅ Notificaciones de alerta creadas: "
        f"{len(usuarios_destino)} para alerta {alerta.id}"
    )

@router.post(
    "/iniciar",
    response_model=SesionAtencionOut,
    status_code=status.HTTP_201_CREATED,
)

def iniciar_sesion(
    data: InicioSesionCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_terapeuta),
):
    paciente = db.query(Paciente).filter(Paciente.id == data.pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    if not _terapeuta_puede_atender_paciente(
        db=db,
        paciente=paciente,
        current_user=current_user,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "No puedes atender este paciente. "
                "Debe estar asignado a ti, compartido contigo o cedido temporalmente."
            ),
        )

    sesion_abierta = (
        db.query(SesionTerapia)
        .filter(
            SesionTerapia.pacienteid == data.pacienteid,
            SesionTerapia.horasalida == None,
        )
        .first()
    )

    if sesion_abierta:
        raise HTTPException(
            status_code=400,
            detail="Este paciente ya tiene una sesión en curso",
        )

    tratamiento_activo = obtener_tratamiento_activo(
        db=db,
        paciente_id=data.pacienteid,
        tratamiento_id=data.tratamientopacienteid,
    )

    validar_sesiones_disponibles_para_tratamiento(
        db=db,
        tratamiento=tratamiento_activo,
    )

    ahora = now_ecuador()
    fecha_actual = ahora.date()

    asistencia_existente = (
        db.query(Asistencia)
        .filter(
            Asistencia.pacienteid == data.pacienteid,
            Asistencia.fecha == fecha_actual,
        )
        .first()
    )

    if not asistencia_existente:
        asistencia = Asistencia(
            pacienteid=data.pacienteid,
            fecha=fecha_actual,
            horaregistro=ahora,
        )
        db.add(asistencia)

    nueva_sesion = SesionTerapia(
        pacienteid=data.pacienteid,
        terapeutaid=current_user.id,
        fecha=fecha_actual,
        horaingreso=ahora.time().replace(microsecond=0),
        horasalida=None,
        escaladolorentrada=data.escaladolorentrada,
        escaladolorsalida=0,
        pacientepaqueteid=None,
        tratamientopacienteid=tratamiento_activo.id,
    )

    db.add(nueva_sesion)
    db.commit()
    db.refresh(nueva_sesion)

    nueva_sesion = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == nueva_sesion.id)
        .first()
    )

    return build_sesion_out(nueva_sesion, db)


@router.get("/en-curso", response_model=List[SesionAtencionOut])
def listar_sesiones_en_curso(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_terapeuta),
):
    sesiones = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(
            SesionTerapia.terapeutaid == current_user.id,
            SesionTerapia.horasalida == None,
        )
        .order_by(SesionTerapia.horaingreso.asc())
        .all()
    )

    return [build_sesion_out(sesion, db) for sesion in sesiones]

@router.get("/tipos-tratamiento", response_model=List[TipoTratamientoOut])
def listar_tipos_tratamiento(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    tratamientos = (
        db.query(TipoTratamiento)
        .filter(TipoTratamiento.activo == True)
        .order_by(TipoTratamiento.nombre.asc())
        .all()
    )

    return tratamientos

@router.put("/{sesion_id}/finalizar", response_model=SesionAtencionOut)
def finalizar_sesion(
    sesion_id: int,
    data: FinalizarSesionCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_terapeuta),
):
    sesion = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == sesion_id)
        .first()
    )

    if not sesion:
        raise HTTPException(
            status_code=404,
            detail="Sesión no encontrada",
        )

    if sesion.terapeutaid != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    if sesion.horasalida is not None:
        raise HTTPException(
            status_code=400,
            detail="La sesión ya fue finalizada",
        )

    if not sesion.tratamientopacienteid:
        raise HTTPException(
            status_code=400,
            detail="La sesión no tiene un tratamiento asociado.",
        )

    ahora = now_ecuador()

    sesion.horasalida = ahora.time().replace(microsecond=0)
    sesion.escaladolorsalida = data.escaladolorsalida

    # Limpia tratamientos aplicados anteriores por seguridad
    db.query(SesionTratamiento).filter(
        SesionTratamiento.sesionid == sesion.id
    ).delete()

    tratamientos_ids = list(set(data.tratamientos or []))

    if tratamientos_ids:
        tratamientos_validos = (
            db.query(TipoTratamiento)
            .filter(
                TipoTratamiento.id.in_(tratamientos_ids),
                TipoTratamiento.activo == True,
            )
            .all()
        )

        ids_validos = {tratamiento.id for tratamiento in tratamientos_validos}

        ids_invalidos = set(tratamientos_ids) - ids_validos

        if ids_invalidos:
            raise HTTPException(
                status_code=400,
                detail=f"Tratamientos inválidos: {list(ids_invalidos)}",
            )

        for tratamiento in tratamientos_validos:
            sesion_tratamiento = SesionTratamiento(
                sesionid=sesion.id,
                tratamientoid=tratamiento.id,
                intensidad=None,
            )
            db.add(sesion_tratamiento)

    paciente = sesion.paciente

    if not paciente:
        paciente = (
            db.query(Paciente)
            .filter(Paciente.id == sesion.pacienteid)
            .first()
        )

    sesion_anterior = (
        db.query(SesionTerapia)
        .filter(
            SesionTerapia.pacienteid == sesion.pacienteid,
            SesionTerapia.id != sesion.id,
            SesionTerapia.horasalida != None,
        )
        .order_by(
            SesionTerapia.fecha.desc(),
            SesionTerapia.horaingreso.desc(),
        )
        .first()
    )

    if sesion_anterior:
        dolor_salida_anterior = sesion_anterior.escaladolorsalida or 0
        aumento = sesion.escaladolorentrada - dolor_salida_anterior

        if aumento >= 2:
            alerta_aumento = Alerta(
                paciente_id=sesion.pacienteid,
                tipo="pain_increase",
                descripcion=(
                    f"Aumento de dolor: "
                    f"{dolor_salida_anterior} → "
                    f"{sesion.escaladolorentrada}"
                ),
            )

            db.add(alerta_aumento)
            db.flush()

            if paciente:
                _notificar_alerta_clinica(
                    db=db,
                    alerta=alerta_aumento,
                    paciente=paciente,
                    sesion=sesion,
                    current_user=current_user,
                )

    if sesion.escaladolorentrada >= 8:
        alerta_dolor_alto = Alerta(
            paciente_id=sesion.pacienteid,
            tipo="high_pain",
            descripcion=f"Dolor crítico: {sesion.escaladolorentrada}/10",
        )

        db.add(alerta_dolor_alto)
        db.flush()

        if paciente:
            _notificar_alerta_clinica(
                db=db,
                alerta=alerta_dolor_alto,
                paciente=paciente,
                sesion=sesion,
                current_user=current_user,
            )

    db.commit()
    db.refresh(sesion)

    sesion = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == sesion.id)
        .first()
    )

    return build_sesion_out(sesion, db)

@router.get("/", response_model=List[SesionAtencionOut])
def listar_sesiones(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(SesionTerapia).options(
        joinedload(SesionTerapia.paciente),
        joinedload(SesionTerapia.tratamiento_paciente),
    )

    if current_user.rol == 2:
        query = query.filter(SesionTerapia.terapeutaid == current_user.id)

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        query = query.join(
            Paciente,
            Paciente.id == SesionTerapia.pacienteid,
        ).filter(
            Paciente.consultorioid == current_user.consultorioid,
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    sesiones = (
        query.order_by(
            SesionTerapia.fecha.desc(),
            SesionTerapia.horaingreso.desc(),
        )
        .all()
    )

    return [build_sesion_out(sesion, db) for sesion in sesiones]