import os
from datetime import date, time, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session, joinedload

from ..services.notificacion_service import crear_notificacion_usuario
from ..utils.fechas import now_ecuador
from ..auth.dependencies import get_current_secretary, get_current_terapeuta, get_current_user
from ..auth.permissions import (
    validar_consultorio_secretario,
    permiso_temporal_activo,
    TIPO_REGISTRO_RETROACTIVO,
    terapeuta_tiene_permiso_atencion_sucursal_temporal,
)
from ..dependencies.db import get_db
from ..models.alerta import Alerta
from ..models.asistencia import Asistencia
from ..models.gimnasio import MovimientoGimnasio
from ..models.paciente import Paciente
from ..models.sesion_terapia import SesionTerapia
from ..models.tratamiento import SesionTratamiento, TipoTratamiento
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..schemas.sesion import (
    FinalizarSesionCreate,
    InicioSesionCreate,
    SesionAtencionOut,
    SesionCambiarTratamientoCreate,
    TipoTratamientoOut,
)
from ..models.paciente_terapeuta_compartido import PacienteTerapeutaCompartido
from ..models.transferencia import Transferencia
from app.models import tratamiento


router = APIRouter(prefix="/api/sesiones", tags=["sesiones"])

# Cantidad máxima de días entre sesiones para considerarlas
# "relativamente seguidas" en el análisis de progreso del dolor.
PAIN_PROGRESS_MAX_GAP_DAYS = int(
    os.getenv("PAIN_PROGRESS_MAX_GAP_DAYS", "14")
)

# Umbral mínimo para activar la alerta de dolor sin reducción.
# 7 coincide con la clasificación visual del front: Bajo 0-3, Medio 4-6, Alto 7-10.
# Así evitamos falsos positivos como 0 -> 0, 3 -> 3 o 6 -> 6.
PAIN_NO_REDUCTION_MIN_LEVEL = int(
    os.getenv("PAIN_NO_REDUCTION_MIN_LEVEL", "7")
)



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
    """
    OPTIMIZADO: antes cargaba TODAS las transferencias + pacientes con
    joinedload y luego iteraba en Python. Ahora hace 1 query EXISTS con JOIN.
    O(n*m) → O(1).
    """
    existe = (
        db.query(Transferencia.id)
        .join(Transferencia.pacientes)
        .filter(
            Transferencia.terapeuta_destino_id == terapeuta_id,
            Transferencia.activo == True,
            Paciente.id == paciente_id,
        )
        .first()
    )
    return existe is not None


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

    # 4. Permiso temporal de atención por sucursal
    if terapeuta_tiene_permiso_atencion_sucursal_temporal(
        db=db,
        paciente=paciente,
        current_user=current_user,
    ):
        return True

    return False


def _terapeuta_asignado_activo_para_paciente(
    db: Session,
    paciente: Paciente,
) -> Usuario:
    if not paciente.terapeutaasignadoid:
        raise HTTPException(
            status_code=400,
            detail=(
                "Este paciente no tiene fisioterapeuta asignado. "
                "Asigne un terapeuta antes de iniciar la atención."
            ),
        )

    terapeuta = (
        db.query(Usuario)
        .filter(
            Usuario.id == paciente.terapeutaasignadoid,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not terapeuta:
        raise HTTPException(
            status_code=400,
            detail=(
                "El fisioterapeuta asignado al paciente no existe "
                "o está inactivo."
            ),
        )

    return terapeuta


def _obtener_terapeuta_registro_atencion(
    db: Session,
    paciente: Paciente,
    current_user: Usuario,
) -> Usuario:
    """
    Define qué terapeuta queda responsable de la sesión.

    - Terapeuta: atiende pacientes propios, compartidos o cedidos.
    - Secretario: puede iniciar por apoyo solo pacientes de su consultorio;
      la sesión queda asignada al terapeuta principal del paciente.
    - Jefe: puede iniciar por apoyo; la sesión queda asignada al terapeuta
      principal del paciente.
    """

    if current_user.rol == 2:
        if not _terapeuta_puede_atender_paciente(
            db=db,
            paciente=paciente,
            current_user=current_user,
        ):
            raise HTTPException(
                status_code=403,
                detail=(
                    "No puedes atender este paciente. "
                    "Debe estar asignado a ti, compartido contigo "
                    "o cedido temporalmente."
                ),
            )

        return current_user

    if current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            paciente.consultorioid,
        )

        return _terapeuta_asignado_activo_para_paciente(db, paciente)

    if current_user.rol == 3:
        return _terapeuta_asignado_activo_para_paciente(db, paciente)

    raise HTTPException(
        status_code=403,
        detail="No autorizado para iniciar atenciones.",
    )


def build_sesion_out(
    sesion: SesionTerapia,
    db: Session,
    tratamientos_aplicados_override: Optional[list[str]] = None,
) -> SesionAtencionOut:
    paciente_nombre = None
    terapeuta_nombre = None
    tratamiento_nombre = None
    precio_sesion_aplicado = None
    tratamientos_aplicados: list[str] = []

    if sesion.paciente:
        paciente_nombre = f"{sesion.paciente.nombres} {sesion.paciente.apellidos}"

    if sesion.terapeuta:
        terapeuta_nombre = f"{sesion.terapeuta.nombres} {sesion.terapeuta.apellidos}".strip()

    # Refuerzo: en algunos listados viejos la relación puede no venir cargada.
    # Si eso pasa, consultamos el usuario directamente para no devolver solo el ID.
    if not terapeuta_nombre and sesion.terapeutaid:
        terapeuta_db = db.query(Usuario).filter(Usuario.id == sesion.terapeutaid).first()
        if terapeuta_db:
            terapeuta_nombre = f"{terapeuta_db.nombres} {terapeuta_db.apellidos}".strip()

    if sesion.tratamiento_paciente:
        tratamiento_nombre = sesion.tratamiento_paciente.tipotratamiento

        if sesion.tratamiento_paciente.precio_sesion_aplicado is not None:
            precio_sesion_aplicado = float(
                sesion.tratamiento_paciente.precio_sesion_aplicado
            )

    if tratamientos_aplicados_override is not None:
        tratamientos_aplicados = tratamientos_aplicados_override
    elif sesion.id:
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
        terapeuta_nombre=terapeuta_nombre,
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
        analisisdolorrequerido=bool(
            getattr(sesion, "analisisdolorrequerido", False)
        ),
        motivodolornodisminuye=getattr(
            sesion,
            "motivodolornodisminuye",
            None,
        ),
        dolorreferenciaprogreso=getattr(
            sesion,
            "dolorreferenciaprogreso",
            None,
        ),
        doloractualprogreso=getattr(
            sesion,
            "doloractualprogreso",
            None,
        ),
    )


def _tratamientos_aplicados_por_sesion(
    db: Session,
    sesiones: list[SesionTerapia],
) -> dict[int, list[str]]:
    sesiones_ids = [sesion.id for sesion in sesiones if sesion.id]

    if not sesiones_ids:
        return {}

    rows = (
        db.query(
            SesionTratamiento.sesionid,
            TipoTratamiento.nombre,
        )
        .join(
            TipoTratamiento,
            TipoTratamiento.id == SesionTratamiento.tratamientoid,
        )
        .filter(SesionTratamiento.sesionid.in_(sesiones_ids))
        .order_by(SesionTratamiento.sesionid.asc(), TipoTratamiento.nombre.asc())
        .all()
    )

    resultado: dict[int, list[str]] = {}

    for sesion_id, nombre in rows:
        if not nombre:
            continue
        resultado.setdefault(sesion_id, []).append(nombre)

    return resultado


def _build_sesiones_out(
    sesiones: list[SesionTerapia],
    db: Session,
) -> list[SesionAtencionOut]:
    tratamientos_por_sesion = _tratamientos_aplicados_por_sesion(db, sesiones)

    return [
        build_sesion_out(
            sesion,
            db,
            tratamientos_aplicados_override=tratamientos_por_sesion.get(sesion.id, []),
        )
        for sesion in sesiones
    ]


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

def _ya_tiene_sesion_en_fecha_para_patologia(
    db: Session,
    paciente_id: int,
    tratamiento: TratamientoPaciente,
    fecha_sesion: date,
) -> bool:
    """
    Valida la regla:
    1 sesión diaria por patología.

    Si el tratamiento tiene diagnosticoid:
        bloquea cualquier sesión del mismo diagnóstico en esa fecha.

    Si no tiene diagnosticoid:
        bloquea por el mismo tratamiento en esa fecha.
    """

    if tratamiento.diagnosticoid is not None:
        return (
            db.query(SesionTerapia.id)
            .join(
                TratamientoPaciente,
                TratamientoPaciente.id == SesionTerapia.tratamientopacienteid,
            )
            .filter(
                SesionTerapia.pacienteid == paciente_id,
                SesionTerapia.fecha == fecha_sesion,
                TratamientoPaciente.diagnosticoid == tratamiento.diagnosticoid,
            )
            .first()
            is not None
        )

    return (
        db.query(SesionTerapia.id)
        .filter(
            SesionTerapia.pacienteid == paciente_id,
            SesionTerapia.fecha == fecha_sesion,
            SesionTerapia.tratamientopacienteid == tratamiento.id,
        )
        .first()
        is not None
    )

def _validar_permiso_registro_retroactivo(
    db: Session,
    current_user: Usuario,
    fecha_sesion: date,
) -> None:
    hoy = now_ecuador().date()

    # Sesión de hoy: no necesita permiso retroactivo.
    if fecha_sesion == hoy:
        return

    if fecha_sesion > hoy:
        raise HTTPException(
            status_code=400,
            detail="No se puede registrar una atención con fecha futura.",
        )

    # Semana CORPOFIT: domingo a sábado.
    # date.weekday(): lunes=0, ..., domingo=6.
    # Días transcurridos desde domingo: domingo=0, lunes=1, ..., sábado=6.
    domingo_semana_actual = hoy - timedelta(days=(hoy.weekday() + 1) % 7)

    if fecha_sesion < domingo_semana_actual:
        raise HTTPException(
            status_code=400,
            detail=(
                "El permiso retroactivo solo permite registrar atenciones "
                "desde el domingo de la semana actual."
            ),
        )

    permiso = permiso_temporal_activo(
        db=db,
        usuario=current_user,
        tipo_permiso=TIPO_REGISTRO_RETROACTIVO,
    )

    if not permiso:
        raise HTTPException(
            status_code=403,
            detail=(
                "No tienes permiso activo para registrar atenciones retroactivas. "
                "Solicita autorización al jefe o secretario."
            ),
        )
    
def _validar_datos_sesion_retroactiva(
    data: InicioSesionCreate,
    fecha_sesion: date,
) -> None:
    hoy = now_ecuador().date()

    es_retroactiva = data.retroactiva or fecha_sesion != hoy

    if not es_retroactiva:
        return

    if data.hora_ingreso is None:
        raise HTTPException(
            status_code=400,
            detail="Debe ingresar la hora de ingreso para la sesión retroactiva.",
        )

    if data.hora_salida is None:
        raise HTTPException(
            status_code=400,
            detail="Debe ingresar la hora de salida para la sesión retroactiva.",
        )

    if data.escaladolorsalida is None:
        raise HTTPException(
            status_code=400,
            detail="Debe ingresar el dolor de salida para la sesión retroactiva.",
        )

    if data.hora_salida <= data.hora_ingreso:
        raise HTTPException(
            status_code=400,
            detail="La hora de salida debe ser mayor a la hora de ingreso.",
        )
    


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


def _nombre_usuario(usuario: Usuario | None) -> str:
    if not usuario:
        return "No asignado"

    nombre = f"{usuario.nombres or ''} {usuario.apellidos or ''}".strip()

    if nombre:
        return nombre

    if getattr(usuario, "email", None):
        return usuario.email

    return f"Usuario {usuario.id}"


def _nombre_tratamiento_sesion(sesion: SesionTerapia | None) -> str:
    if not sesion or not getattr(sesion, "tratamiento_paciente", None):
        return "Tratamiento no especificado"

    tratamiento = sesion.tratamiento_paciente

    for attr in ("tipotratamiento", "nombre", "descripcion"):
        valor = getattr(tratamiento, attr, None)
        if valor:
            return str(valor).strip()

    return f"Tratamiento #{getattr(tratamiento, 'id', '')}".strip()


def _texto_corto(texto: str | None, max_len: int = 180) -> str:
    limpio = (texto or "").strip()

    if len(limpio) <= max_len:
        return limpio

    return limpio[: max_len - 1].rstrip() + "…"


def _formatear_fecha_corta(fecha: date | None) -> str:
    if fecha is None:
        return "sin fecha"

    return fecha.strftime("%d/%m/%Y")


def _agregar_usuario_unico(
    usuarios: list[Usuario],
    usuarios_ids: set[int],
    usuario: Usuario | None,
) -> None:
    if not usuario:
        return

    if usuario.activo is not True:
        return

    if usuario.id in usuarios_ids:
        return

    usuarios.append(usuario)
    usuarios_ids.add(usuario.id)


def _obtener_terapeutas_a_cargo_paciente(
    db: Session,
    paciente: Paciente,
    sesion: SesionTerapia | None = None,
) -> list[Usuario]:
    """
    Versión optimizada: resuelve todos los terapeutas en 1 sola query
    usando UNION en lugar de 3-4 queries separadas.
    """
    from sqlalchemy import union_all, literal_column

    hoy = now_ecuador().date()

    # IDs a resolver en una sola query final
    terapeuta_ids: set[int] = set()

    # 1. Terapeuta principal asignado
    if paciente.terapeutaasignadoid:
        terapeuta_ids.add(paciente.terapeutaasignadoid)

    # 2. Terapeuta que atendió la sesión
    if sesion and sesion.terapeutaid:
        terapeuta_ids.add(sesion.terapeutaid)

    # 3. Terapeutas compartidos activos — 1 query
    compartidos_ids = [
        row.terapeutaid
        for row in db.query(PacienteTerapeutaCompartido.terapeutaid)
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente.id,
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
        .all()
    ]
    terapeuta_ids.update(compartidos_ids)

    # 4. Terapeutas por cesión — 1 query con JOIN en vez de cargar
    #    todas las transferencias con joinedload
    cedidos_ids = [
        row.terapeuta_destino_id
        for row in db.query(Transferencia.terapeuta_destino_id)
        .join(
            Transferencia.pacientes
        )
        .filter(
            Transferencia.activo == True,
            Paciente.id == paciente.id,
        )
        .all()
    ]
    terapeuta_ids.update(cedidos_ids)

    if not terapeuta_ids:
        return []

    # 1 sola query final para traer todos los objetos Usuario
    return (
        db.query(Usuario)
        .filter(
            Usuario.id.in_(list(terapeuta_ids)),
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .all()
    )


def _obtener_usuarios_para_alerta(
    db: Session,
    paciente: Paciente,
    sesion: SesionTerapia | None = None,
) -> list[Usuario]:
    usuarios: list[Usuario] = []
    usuarios_ids: set[int] = set()

    # 1. Terapeutas a cargo del paciente.
    terapeutas = _obtener_terapeutas_a_cargo_paciente(
        db=db,
        paciente=paciente,
        sesion=sesion,
    )

    for terapeuta in terapeutas:
        _agregar_usuario_unico(
            usuarios,
            usuarios_ids,
            terapeuta,
        )

    # 2 + 3. Secretarios del consultorio + Jefes — 1 sola query con OR en rol.
    # Antes eran 2 queries separadas.
    secretarios_y_jefes = (
        db.query(Usuario)
        .filter(
            Usuario.activo == True,
            or_(
                and_(Usuario.rol == 1, Usuario.consultorioid == paciente.consultorioid),
                Usuario.rol == 3,
            ),
        )
        .all()
    )

    for usuario_extra in secretarios_y_jefes:
        _agregar_usuario_unico(usuarios, usuarios_ids, usuario_extra)

    return usuarios


def _notificar_alerta_clinica(
    db: Session,
    alerta: Alerta,
    paciente: Paciente,
    sesion: SesionTerapia,
    current_user: Usuario,
    terapeutas_a_cargo: list[Usuario] | None = None,  # <- nuevo parámetro
) -> None:
    if terapeutas_a_cargo is None:
        terapeutas_a_cargo = _obtener_terapeutas_a_cargo_paciente(
            db=db, paciente=paciente, sesion=sesion,
        )
    nombre_paciente = _nombre_paciente(paciente)
    nombre_terapeuta_sesion = _nombre_usuario(current_user)

    nombres_terapeutas_a_cargo = [
        _nombre_usuario(terapeuta)
        for terapeuta in terapeutas_a_cargo
    ]

    terapeutas_texto = (
        ", ".join(nombres_terapeutas_a_cargo)
        if nombres_terapeutas_a_cargo
        else "No asignado"
    )

    if alerta.tipo == "high_pain":
        titulo = "Alerta clínica: dolor crítico"
        mensaje = (
            f"Paciente: {nombre_paciente}\n"
            f"Dolor crítico: {sesion.escaladolorentrada}/10\n"
            f"Atendió: {nombre_terapeuta_sesion}\n"
            f"Terapeuta(s) a cargo: {terapeutas_texto}"
        )

    elif alerta.tipo == "pain_no_reduction":
        tratamiento_nombre = _nombre_tratamiento_sesion(sesion)
        motivo = _texto_corto(
            getattr(sesion, "motivodolornodisminuye", None),
            max_len=180,
        )
        dolor_referencia = getattr(sesion, "dolorreferenciaprogreso", None)
        dolor_actual = getattr(sesion, "doloractualprogreso", None)

        titulo = "Seguimiento clínico: dolor sin reducción"
        mensaje = (
            f"Paciente: {nombre_paciente}\n"
            f"Tratamiento: {tratamiento_nombre}\n"
            f"Dolor en 3 terapias: {dolor_referencia}/10 → {dolor_actual}/10\n"
            f"Motivo registrado: {motivo or 'No especificado'}\n"
            f"Registró: {nombre_terapeuta_sesion}\n"
            f"Fisio(s) a cargo: {terapeutas_texto}"
        )

    elif alerta.tipo == "pain_increase":
        # Compatibilidad con alertas antiguas. El flujo nuevo ya no crea
        # alertas por cada aumento aislado de dolor.
        titulo = "Alerta clínica: aumento de dolor"
        mensaje = (
            f"Paciente: {nombre_paciente}\n"
            f"{alerta.descripcion}\n"
            f"Atendió: {nombre_terapeuta_sesion}\n"
            f"Terapeuta(s) a cargo: {terapeutas_texto}"
        )

    else:
        titulo = "Alerta clínica"
        mensaje = (
            f"Paciente: {nombre_paciente}\n"
            f"{alerta.descripcion}\n"
            f"Atendió: {nombre_terapeuta_sesion}\n"
            f"Terapeuta(s) a cargo: {terapeutas_texto}"
        )

    # OPTIMIZADO: reutiliza los terapeutas_a_cargo ya calculados en el contexto
    # llamante para no volver a calcular terapeutas compartidos/cedidos.
    # _obtener_usuarios_para_alerta solo se llama si no se pasaron terapeutas.
    if terapeutas_a_cargo is not None:
        # Armar la lista completa: terapeutas + secretarios del consultorio + jefes
        usuarios_destino_ids: set[int] = {t.id for t in terapeutas_a_cargo}
        secretarios_y_jefes_alerta = (
            db.query(Usuario)
            .filter(
                Usuario.activo == True,
                or_(
                    and_(Usuario.rol == 1, Usuario.consultorioid == paciente.consultorioid),
                    Usuario.rol == 3,
                ),
            )
            .all()
        )
        usuarios_destino = list(terapeutas_a_cargo)
        for u in secretarios_y_jefes_alerta:
            if u.id not in usuarios_destino_ids:
                usuarios_destino.append(u)
                usuarios_destino_ids.add(u.id)
    else:
        usuarios_destino = _obtener_usuarios_para_alerta(
            db=db,
            paciente=paciente,
            sesion=sesion,
        )

    if not usuarios_destino:
        print("⚠️ No se encontraron usuarios destino para la alerta clínica.")
        return

    notificaciones_creadas = 0

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
                "paciente_nombre": nombre_paciente,
                "sesion_id": sesion.id,
                "consultorioid": paciente.consultorioid,
                "tipo_alerta": alerta.tipo,
                "descripcion": alerta.descripcion,
                "dolor_entrada": sesion.escaladolorentrada,
                "dolor_salida": sesion.escaladolorsalida,
                "terapeuta_id": sesion.terapeutaid,
                "terapeuta_nombre": nombre_terapeuta_sesion,
                "tratamiento_nombre": _nombre_tratamiento_sesion(sesion),
                "motivo_dolor_no_disminuye": getattr(
                    sesion,
                    "motivodolornodisminuye",
                    None,
                ),
                "dolor_referencia_progreso": getattr(
                    sesion,
                    "dolorreferenciaprogreso",
                    None,
                ),
                "dolor_actual_progreso": getattr(
                    sesion,
                    "doloractualprogreso",
                    None,
                ),
                "terapeutas_a_cargo": nombres_terapeutas_a_cargo,
                "terapeutas_a_cargo_ids": [
                    terapeuta.id for terapeuta in terapeutas_a_cargo
                ],
                "creado_por_id": current_user.id,
                "actualizar": [
                    "alertas",
                    "dashboard",
                    "notificaciones",
                ],
            },
            hacer_flush=False,
        )

        notificaciones_creadas += 1

    db.flush()

    print(
        f"✅ Notificaciones de alerta creadas: "
        f"{notificaciones_creadas} para alerta {alerta.id}"
    )


def _dolor_final_sesion(sesion: SesionTerapia) -> int:
    if sesion.escaladolorsalida is not None:
        return int(sesion.escaladolorsalida)

    return int(sesion.escaladolorentrada or 0)


def _analizar_dolor_ultimas_tres_sesiones(
    db: Session,
    sesion_actual: SesionTerapia,
    dolor_salida_actual: int,
) -> Optional[dict]:
    """
    Consulta liviana: solo toma las 2 sesiones finalizadas anteriores
    del mismo tratamiento y paciente. Con la actual forman las últimas 3.

    Si entre sesiones hay una separación mayor a PAIN_PROGRESS_MAX_GAP_DAYS,
    no se dispara la alerta porque no se consideran relativamente seguidas.
    """

    if not sesion_actual.tratamientopacienteid:
        return None

    anteriores_desc = (
        db.query(SesionTerapia)
        .filter(
            SesionTerapia.pacienteid == sesion_actual.pacienteid,
            SesionTerapia.tratamientopacienteid == sesion_actual.tratamientopacienteid,
            SesionTerapia.id != sesion_actual.id,
            SesionTerapia.horasalida != None,
        )
        .order_by(
            SesionTerapia.fecha.desc(),
            SesionTerapia.horaingreso.desc(),
        )
        .limit(2)
        .all()
    )

    if len(anteriores_desc) < 2:
        return None

    sesiones = list(reversed(anteriores_desc)) + [sesion_actual]

    for index in range(1, len(sesiones)):
        dias = (sesiones[index].fecha - sesiones[index - 1].fecha).days

        if dias < 0 or dias > PAIN_PROGRESS_MAX_GAP_DAYS:
            return None

    dolor_referencia = _dolor_final_sesion(sesiones[0])
    dolor_actual = int(dolor_salida_actual)

    # No generar alerta cuando el dolor actual está en nivel bajo o medio.
    # Antes 0 -> 0, 3 -> 3 o 6 -> 6 disparaban la alerta porque
    # matemáticamente "no disminuyeron", aunque clínicamente no son dolor alto.
    if dolor_actual < PAIN_NO_REDUCTION_MIN_LEVEL:
        return None

    if dolor_actual < dolor_referencia:
        return None

    return {
        "dolor_referencia": dolor_referencia,
        "dolor_actual": dolor_actual,
        "fecha_referencia": sesiones[0].fecha.isoformat(),
        "fecha_actual": sesion_actual.fecha.isoformat(),
        "max_gap_days": PAIN_PROGRESS_MAX_GAP_DAYS,
        "min_pain_level": PAIN_NO_REDUCTION_MIN_LEVEL,
    }


def _normalizar_motivo_dolor_no_disminuye(motivo: Optional[str]) -> Optional[str]:
    if motivo is None:
        return None

    texto = motivo.strip()

    if not texto:
        return None

    return texto[:600]


def _exigir_motivo_dolor_no_disminuye_si_corresponde(
    db: Session,
    sesion: SesionTerapia,
    data: FinalizarSesionCreate,
) -> Optional[dict]:
    analisis = _analizar_dolor_ultimas_tres_sesiones(
        db=db,
        sesion_actual=sesion,
        dolor_salida_actual=data.escaladolorsalida,
    )

    if not analisis:
        sesion.analisisdolorrequerido = False
        sesion.motivodolornodisminuye = None
        sesion.dolorreferenciaprogreso = None
        sesion.doloractualprogreso = None
        return None

    motivo = _normalizar_motivo_dolor_no_disminuye(
        data.motivo_dolor_no_disminuye
    )

    if not motivo:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "MOTIVO_DOLOR_NO_DISMINUYE_REQUERIDO",
                "message": (
                    "El dolor no ha disminuido en las últimas 3 terapias "
                    "relativamente seguidas. Registre un motivo clínico "
                    "antes de finalizar la atención."
                ),
                "dolor_referencia": analisis["dolor_referencia"],
                "dolor_actual": analisis["dolor_actual"],
                "max_gap_days": analisis["max_gap_days"],
                "min_pain_level": analisis.get(
                    "min_pain_level",
                    PAIN_NO_REDUCTION_MIN_LEVEL,
                ),
            },
        )

    sesion.analisisdolorrequerido = True
    sesion.motivodolornodisminuye = motivo
    sesion.dolorreferenciaprogreso = analisis["dolor_referencia"]
    sesion.doloractualprogreso = analisis["dolor_actual"]

    return analisis


def _crear_alerta_dolor_no_disminuye(
    db: Session,
    sesion: SesionTerapia,
    paciente: Paciente | None,
    current_user: Usuario,
    analisis: dict | None,
    terapeutas_a_cargo: list[Usuario] | None = None,  # <- nuevo parámetro
) -> None:
    if not paciente or not analisis:
        return

    motivo = _normalizar_motivo_dolor_no_disminuye(
        sesion.motivodolornodisminuye
    ) or "No especificado"

    descripcion = (
        "Dolor sin reducción en las últimas 3 terapias seguidas. "
        f"Dolor referencia: {analisis['dolor_referencia']}/10 "
        f"({_formatear_fecha_corta(sesion.fecha if not analisis.get('fecha_referencia') else datetime.fromisoformat(analisis['fecha_referencia']).date())}); "
        f"dolor actual: {analisis['dolor_actual']}/10 "
        f"({_formatear_fecha_corta(sesion.fecha)}). "
        f"Motivo registrado: {_texto_corto(motivo, max_len=220)}"
    )

    alerta = Alerta(
        paciente_id=sesion.pacienteid,
        tipo="pain_no_reduction",
        descripcion=_texto_corto(descripcion, max_len=490),
    )

    db.add(alerta)
    db.flush()

    _notificar_alerta_clinica(
        db=db,
        alerta=alerta,
        paciente=paciente,
        sesion=sesion,
        current_user=current_user,
        terapeutas_a_cargo=terapeutas_a_cargo,  # <- pasa el parámetro
    )




def _secretario_puede_gestionar_sesion_compartida(
    current_user: Usuario,
    sesion: SesionTerapia | None = None,
    paciente: Paciente | None = None,
    db: Session | None = None,
) -> bool:
    if current_user.rol != 1 or current_user.consultorioid is None or db is None:
        return False

    if paciente is not None and paciente.consultorioid == current_user.consultorioid:
        return True

    if sesion is not None:
        terapeuta_sesion = (
            db.query(Usuario.id)
            .filter(
                Usuario.id == sesion.terapeutaid,
                Usuario.consultorioid == current_user.consultorioid,
            )
            .first()
        )
        if terapeuta_sesion is not None:
            return True

    if paciente is not None:
        hoy = now_ecuador().date()
        compartido = (
            db.query(PacienteTerapeutaCompartido.id)
            .join(Usuario, Usuario.id == PacienteTerapeutaCompartido.terapeutaid)
            .filter(
                PacienteTerapeutaCompartido.pacienteid == paciente.id,
                PacienteTerapeutaCompartido.activo == True,
                Usuario.consultorioid == current_user.consultorioid,
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
        if compartido is not None:
            return True

    return False


def _validar_gestor_sesion_paciente(
    current_user: Usuario,
    paciente: Paciente | None,
    sesion: SesionTerapia | None = None,
    db: Session | None = None,
) -> None:
    if current_user.rol == 3:
        return

    if current_user.rol == 1:
        if paciente is None:
            raise HTTPException(
                status_code=404,
                detail="Paciente no encontrado.",
            )

        if _secretario_puede_gestionar_sesion_compartida(
            current_user=current_user,
            sesion=sesion,
            paciente=paciente,
            db=db,
        ):
            return

        validar_consultorio_secretario(
            current_user,
            paciente.consultorioid,
        )
        return

    raise HTTPException(
        status_code=403,
        detail="Solo secretario o jefe pueden corregir atenciones.",
    )


def _validar_tratamiento_para_sesion(
    db: Session,
    tratamiento_id: int,
    paciente_id: int,
) -> TratamientoPaciente:
    tratamiento = (
        db.query(TratamientoPaciente)
        .filter(
            TratamientoPaciente.id == tratamiento_id,
            TratamientoPaciente.pacienteid == paciente_id,
        )
        .first()
    )

    if not tratamiento:
        raise HTTPException(
            status_code=400,
            detail="El tratamiento seleccionado no pertenece a este paciente.",
        )

    return tratamiento


@router.post(
    "/iniciar",
    response_model=SesionAtencionOut,
    status_code=status.HTTP_201_CREATED,
)

def iniciar_sesion(
    data: InicioSesionCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = db.query(Paciente).filter(Paciente.id == data.pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    terapeuta_responsable = _obtener_terapeuta_registro_atencion(
        db=db,
        paciente=paciente,
        current_user=current_user,
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

    ahora = now_ecuador()
    fecha_actual = ahora.date()
    fecha_sesion = data.fecha_atencion or fecha_actual
    es_retroactiva = data.retroactiva or fecha_sesion != fecha_actual

    _validar_permiso_registro_retroactivo(
        db=db,
        current_user=current_user,
        fecha_sesion=fecha_sesion,
    )

    _validar_datos_sesion_retroactiva(
        data=data,
        fecha_sesion=fecha_sesion,
    )

    if _ya_tiene_sesion_en_fecha_para_patologia(
        db=db,
        paciente_id=paciente.id,
        tratamiento=tratamiento_activo,
        fecha_sesion=fecha_sesion,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Este paciente ya tiene una sesión registrada en esa fecha "
                "para esta patología. Solo se permite una sesión diaria por patología."
            ),
        )

    validar_sesiones_disponibles_para_tratamiento(
        db=db,
        tratamiento=tratamiento_activo,
    )

    asistencia_existente = (
        db.query(Asistencia)
        .filter(
            Asistencia.pacienteid == data.pacienteid,
            Asistencia.fecha == fecha_sesion,
        )
        .first()
    )

    if not asistencia_existente:
        asistencia = Asistencia(
            pacienteid=data.pacienteid,
            fecha=fecha_sesion,
            horaregistro=ahora,
        )
        db.add(asistencia)

    hora_ingreso = (
        data.hora_ingreso
        if es_retroactiva and data.hora_ingreso is not None
        else ahora.time().replace(microsecond=0)
    )

    hora_salida = (
        data.hora_salida
        if es_retroactiva
        else None
    )

    dolor_salida = (
        data.escaladolorsalida
        if es_retroactiva and data.escaladolorsalida is not None
        else 0
    )

    nueva_sesion = SesionTerapia(
        pacienteid=data.pacienteid,
        terapeutaid=terapeuta_responsable.id,
        fecha=fecha_sesion,
        horaingreso=hora_ingreso,
        horasalida=hora_salida,
        escaladolorentrada=data.escaladolorentrada,
        escaladolorsalida=dolor_salida,
        pacientepaqueteid=None,
        tratamientopacienteid=tratamiento_activo.id,
    )

    db.add(nueva_sesion)
    db.flush()  # obtiene el ID sin cerrar la transacción

    # Carga las relaciones necesarias para build_sesion_out en la misma
    # transacción, sin hacer un SELECT extra post-commit.
    nueva_sesion = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.terapeuta),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == nueva_sesion.id)
        .first()
    )

    # Construir la respuesta ANTES del commit para evitar que expire_on_commit
    # invalide los atributos del objeto y fuerce recargas innecesarias a la DB.
    # Una sesión recién iniciada no tiene tratamientos aplicados todavía,
    # así que [] evita además el SELECT a sesiontratamiento.
    respuesta = build_sesion_out(
        nueva_sesion,
        db,
        tratamientos_aplicados_override=[],
    )

    db.commit()

    return respuesta


@router.get("/en-curso", response_model=List[SesionAtencionOut])
def listar_sesiones_en_curso(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(SesionTerapia).options(
        joinedload(SesionTerapia.paciente),
        joinedload(SesionTerapia.terapeuta),
        joinedload(SesionTerapia.tratamiento_paciente),
    ).filter(SesionTerapia.horasalida == None)

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
        ).filter(Paciente.consultorioid == current_user.consultorioid)

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para consultar sesiones en curso.",
        )

    sesiones = query.order_by(
        SesionTerapia.fecha.asc(),
        SesionTerapia.horaingreso.asc(),
    ).limit(120).all()

    return _build_sesiones_out(sesiones, db)

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
            joinedload(SesionTerapia.terapeuta),
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

    analisis_dolor = _exigir_motivo_dolor_no_disminuye_si_corresponde(
        db=db,
        sesion=sesion,
        data=data,
    )

    # Calculamos los terapeutas UNA sola vez y los reutilizamos
    # en _crear_alerta y _notificar para no repetir las queries.
    terapeutas_a_cargo = _obtener_terapeutas_a_cargo_paciente(
        db=db,
        paciente=paciente,
        sesion=sesion,
    )

    _crear_alerta_dolor_no_disminuye(
        db=db,
        sesion=sesion,
        paciente=paciente,
        current_user=current_user,
        analisis=analisis_dolor,
        terapeutas_a_cargo=terapeutas_a_cargo,  # <- pasamos el resultado
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
                terapeutas_a_cargo=terapeutas_a_cargo,  # <- reutilizamos
            )

    db.commit()
    db.refresh(sesion)

    sesion = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.terapeuta),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == sesion.id)
        .first()
    )

    return build_sesion_out(sesion, db)



@router.put("/{sesion_id}/cambiar-tratamiento", response_model=SesionAtencionOut)
def cambiar_tratamiento_sesion(
    sesion_id: int,
    data: SesionCambiarTratamientoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    sesion = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.terapeuta),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == sesion_id)
        .first()
    )

    if not sesion:
        raise HTTPException(
            status_code=404,
            detail="Sesión no encontrada.",
        )

    paciente = sesion.paciente
    if not paciente:
        paciente = db.query(Paciente).filter(Paciente.id == sesion.pacienteid).first()

    _validar_gestor_sesion_paciente(current_user=current_user, paciente=paciente, sesion=sesion, db=db)

    tratamiento_nuevo = _validar_tratamiento_para_sesion(
        db=db,
        tratamiento_id=data.tratamientopacienteid,
        paciente_id=sesion.pacienteid,
    )

    tratamiento_anterior_id = sesion.tratamientopacienteid

    if tratamiento_anterior_id == tratamiento_nuevo.id:
        return build_sesion_out(sesion, db)

    sesion.tratamientopacienteid = tratamiento_nuevo.id

    # Si esta terapia también generó un movimiento de gimnasio por reemplazo,
    # mantenemos la referencia al tratamiento correcta para que reportes y saldos
    # no queden cruzados.
    db.query(MovimientoGimnasio).filter(
        MovimientoGimnasio.sesionid == sesion.id
    ).update(
        {MovimientoGimnasio.tratamientopacienteid: tratamiento_nuevo.id},
        synchronize_session=False,
    )

    db.commit()

    sesion_actualizada = (
        db.query(SesionTerapia)
        .options(
            joinedload(SesionTerapia.paciente),
            joinedload(SesionTerapia.terapeuta),
            joinedload(SesionTerapia.tratamiento_paciente),
        )
        .filter(SesionTerapia.id == sesion_id)
        .first()
    )

    return build_sesion_out(sesion_actualizada, db)


@router.delete("/{sesion_id}")
def eliminar_sesion_terapia(
    sesion_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    sesion = (
        db.query(SesionTerapia)
        .options(joinedload(SesionTerapia.paciente), joinedload(SesionTerapia.terapeuta))
        .filter(SesionTerapia.id == sesion_id)
        .first()
    )

    if not sesion:
        raise HTTPException(
            status_code=404,
            detail="Sesión no encontrada.",
        )

    paciente = sesion.paciente
    if not paciente:
        paciente = db.query(Paciente).filter(Paciente.id == sesion.pacienteid).first()

    _validar_gestor_sesion_paciente(current_user=current_user, paciente=paciente, sesion=sesion, db=db)

    paciente_id = sesion.pacienteid
    fecha_sesion = sesion.fecha

    # Primero limpiamos las terapias aplicadas de la sesión para evitar
    # errores por llaves foráneas.
    db.query(SesionTratamiento).filter(
        SesionTratamiento.sesionid == sesion.id
    ).delete(synchronize_session=False)

    # Los movimientos de gimnasio asociados a una sesión usan ON DELETE SET NULL
    # en la DB, pero lo hacemos explícito para que funcione igual en todos los
    # ambientes y no se pierda la asistencia de gimnasio si existía.
    db.query(MovimientoGimnasio).filter(
        MovimientoGimnasio.sesionid == sesion.id
    ).update(
        {MovimientoGimnasio.sesionid: None},
        synchronize_session=False,
    )

    db.delete(sesion)
    db.flush()

    sesiones_restantes = (
        db.query(SesionTerapia.id)
        .filter(
            SesionTerapia.pacienteid == paciente_id,
            SesionTerapia.fecha == fecha_sesion,
        )
        .first()
    )

    movimientos_restantes = (
        db.query(MovimientoGimnasio.id)
        .filter(
            MovimientoGimnasio.pacienteid == paciente_id,
            MovimientoGimnasio.fecha == fecha_sesion,
        )
        .first()
    )

    if not sesiones_restantes and not movimientos_restantes:
        db.query(Asistencia).filter(
            Asistencia.pacienteid == paciente_id,
            Asistencia.fecha == fecha_sesion,
        ).delete(synchronize_session=False)

    db.commit()

    return {
        "ok": True,
        "message": "Atención eliminada correctamente.",
        "sesionid": sesion_id,
    }


@router.get("/", response_model=List[SesionAtencionOut])
def listar_sesiones(
    paciente_id: Optional[int] = Query(default=None, ge=1),
    limit: int = Query(default=80, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(SesionTerapia).options(
        joinedload(SesionTerapia.paciente),
        joinedload(SesionTerapia.terapeuta),
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

    if paciente_id is not None:
        query = query.filter(SesionTerapia.pacienteid == paciente_id)

    sesiones = (
        query.order_by(
            SesionTerapia.fecha.desc(),
            SesionTerapia.horaingreso.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    return _build_sesiones_out(sesiones, db)

@router.get("/tratamiento-resumen/{tratamiento_paciente_id}")
def obtener_resumen_tratamiento_sesion(
    tratamiento_paciente_id: int,
    fecha_atencion: Optional[date] = None,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    tratamiento = (
        db.query(TratamientoPaciente)
        .filter(
            TratamientoPaciente.id == tratamiento_paciente_id,
            TratamientoPaciente.activo == True,
        )
        .first()
    )

    if not tratamiento:
        raise HTTPException(
            status_code=404,
            detail="Tratamiento no encontrado o inactivo.",
        )

    paciente = (
        db.query(Paciente)
        .filter(Paciente.id == tratamiento.pacienteid)
        .first()
    )

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado.",
        )

    if current_user.rol == 2:
        if not _terapeuta_puede_atender_paciente(
            db=db,
            paciente=paciente,
            current_user=current_user,
        ):
            raise HTTPException(
                status_code=403,
                detail="No autorizado para consultar este tratamiento.",
            )

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            paciente.consultorioid,
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado para consultar este tratamiento.",
        )

    sesiones_realizadas = (
        db.query(SesionTerapia.id)
        .filter(
            SesionTerapia.tratamientopacienteid == tratamiento.id,
            SesionTerapia.pacienteid == tratamiento.pacienteid,
            SesionTerapia.horasalida != None,
        )
        .count()
    )

    sesiones_estimadas = tratamiento.sesiones_estimadas

    sesiones_restantes = None
    if sesiones_estimadas is not None:
        sesiones_restantes = max(sesiones_estimadas - sesiones_realizadas, 0)

    fecha_validacion = fecha_atencion or now_ecuador().date()

    tiene_sesion_fecha = _ya_tiene_sesion_en_fecha_para_patologia(
        db=db,
        paciente_id=tratamiento.pacienteid,
        tratamiento=tratamiento,
        fecha_sesion=fecha_validacion,
    )

    return {
        "tratamientopacienteid": tratamiento.id,
        "pacienteid": tratamiento.pacienteid,
        "sesiones_estimadas": sesiones_estimadas,
        "sesiones_realizadas": sesiones_realizadas,
        "sesiones_restantes": sesiones_restantes,
        "fecha_validacion": fecha_validacion.isoformat(),
        "tiene_sesion_fecha": tiene_sesion_fecha,
        "bloqueado_hoy": tiene_sesion_fecha,
        "bloqueado_fecha": tiene_sesion_fecha,
        "mensaje_bloqueo": (
            "Ya se registró una sesión en esa fecha para esta patología."
            if tiene_sesion_fecha
            else None
        ),
    }