from datetime import date, timedelta, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from ..models.pago import Pago
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..auth.permissions import validar_acceso_paciente_por_rol
from ..auth.dependencies import get_current_user
from ..dependencies.db import get_db
from ..models.gimnasio import MembresiaGimnasio, MovimientoGimnasio
from ..models.consultorio import Consultorio
from ..models.usuario_permiso_temporal import UsuarioPermisoTemporal
from ..models.paciente import Paciente
from ..models.paciente_terapeuta_compartido import PacienteTerapeutaCompartido
from ..models.sesion_terapia import SesionTerapia
from ..models.usuario import Usuario
from ..services.notificacion_service import crear_notificacion_usuario
from ..schemas.gimnasio import (
    MembresiaGimnasioCreate,
    MembresiaGimnasioOut,
    MovimientoGimnasioCreate,
    MovimientoGimnasioOut,
    ResumenMembresiaGimnasioOut,
    PaseDiarioGimnasioOut,
    PaseDiarioGimnasioCreate,
    GimnasioAsistenciaRapidaCreate,
    GimnasioAsistenciaRapidaOut,
    GimnasioResponsableOut,
)

router = APIRouter(prefix="/api/gimnasio", tags=["gimnasio"])


TIPO_ASISTENCIA_GIMNASIO = 1
TIPO_TERAPIA_REEMPLAZA_GIMNASIO = 2

MODALIDAD_MENSUAL = "MENSUAL"
MODALIDAD_DIARIA = "DIARIA"

def fecha_ecuador() -> date:
    return datetime.now(timezone(timedelta(hours=-5))).date()


def now_ecuador() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))


def _es_transferencia(metodo: str) -> bool:
    return "transfer" in (metodo or "").strip().lower()

def _es_dia_habil(fecha: date) -> bool:
    return fecha.weekday() < 5


def _contar_dias_habiles(desde: date, hasta: date) -> int:
    if hasta < desde:
        return 0

    total = 0
    actual = desde

    while actual <= hasta:
        if _es_dia_habil(actual):
            total += 1
        actual += timedelta(days=1)

    return total


def _sumar_dias_habiles_incluyendo_inicio(inicio: date, cantidad: int) -> date:
    if cantidad <= 0:
        return inicio

    actual = inicio
    contados = 0

    while True:
        if _es_dia_habil(actual):
            contados += 1

            if contados == cantidad:
                return actual

        actual += timedelta(days=1)


def _validar_acceso_paciente(
    db: Session,
    paciente_id: int,
    current_user: Usuario,
) -> Paciente:
    paciente = db.query(Paciente).filter(Paciente.id == paciente_id).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado.",
        )

    try:
        validar_acceso_paciente_por_rol(
            paciente=paciente,
            current_user=current_user,
            db=db,
        )
    except HTTPException as exc:
        # Regla especial de gimnasio: un secretario de una clínica receptora
        # puede crear/consultar gimnasio si el paciente está compartido con
        # un fisioterapeuta de su consultorio.
        if current_user.rol == 1 and current_user.consultorioid is not None:
            hoy = fecha_ecuador()
            compartido_en_consultorio = (
                db.query(PacienteTerapeutaCompartido.id)
                .join(Usuario, Usuario.id == PacienteTerapeutaCompartido.terapeutaid)
                .filter(
                    PacienteTerapeutaCompartido.pacienteid == paciente.id,
                    PacienteTerapeutaCompartido.activo == True,
                    Usuario.rol == 2,
                    Usuario.activo == True,
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

            if compartido_en_consultorio is not None:
                return paciente

        raise exc

    return paciente



def _nombre_paciente(paciente: Paciente | None) -> str:
    if not paciente:
        return "Paciente"

    return f"{paciente.nombres or ''} {paciente.apellidos or ''}".strip() or "Paciente"




def _nombre_consultorio(consultorio: Consultorio | None) -> str:
    if not consultorio:
        return "Sin consultorio"
    return consultorio.nombre or "Sin consultorio"


def _nombre_consultorio_por_id(db: Session, consultorio_id: int | None) -> str:
    if consultorio_id is None:
        return "Sin consultorio"

    consultorio = db.query(Consultorio).filter(Consultorio.id == consultorio_id).first()
    return _nombre_consultorio(consultorio)


def _resolver_consultorio_operativo_gimnasio(
    *,
    paciente: Paciente,
    current_user: Usuario,
    consultorioid: int | None = None,
) -> int | None:
    """Define a qué clínica pertenece el gimnasio.

    Secretario: siempre su consultorio.
    Jefe: puede enviar consultorioid; si no lo envía, se usa su consultorio
    y, como último respaldo, el consultorio de origen del paciente.
    """
    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        if consultorioid is not None and consultorioid != current_user.consultorioid:
            raise HTTPException(
                status_code=403,
                detail="El gimnasio solo puede crearse para el consultorio del secretario.",
            )

        return current_user.consultorioid

    if current_user.rol == 3:
        return consultorioid or current_user.consultorioid or paciente.consultorioid

    return current_user.consultorioid or paciente.consultorioid


def _agregar_responsable_unico(
    responsables: list[tuple[Usuario, str]],
    ids: set[int],
    usuario: Usuario | None,
    motivo: str,
) -> None:
    if not usuario:
        return
    if usuario.activo is not True or usuario.rol != 2:
        return
    if usuario.id in ids:
        return

    responsables.append((usuario, motivo))
    ids.add(usuario.id)


def _responsables_gimnasio_autorizados(
    *,
    db: Session,
    paciente: Paciente,
    consultorioid: int | None,
    fecha_referencia: date | None = None,
) -> list[tuple[Usuario, str]]:
    """Fisioterapeutas que pueden quedar como responsables de gimnasio.

    Regla: no se listan todos los fisios de la clínica. Solo se permite:
    - terapeuta principal si pertenece al consultorio operativo;
    - terapeutas con el paciente compartido/cedido en ese consultorio;
    - terapeutas con permiso temporal de atención de sucursal en ese consultorio.
    """
    hoy = fecha_referencia or fecha_ecuador()
    responsables: list[tuple[Usuario, str]] = []
    ids: set[int] = set()

    if consultorioid is None:
        return responsables

    if paciente.terapeutaasignadoid:
        terapeuta_principal = (
            db.query(Usuario)
            .filter(
                Usuario.id == paciente.terapeutaasignadoid,
                Usuario.rol == 2,
                Usuario.activo == True,
                Usuario.consultorioid == consultorioid,
            )
            .first()
        )
        _agregar_responsable_unico(
            responsables,
            ids,
            terapeuta_principal,
            "Terapeuta principal",
        )

    compartidos = (
        db.query(Usuario)
        .join(
            PacienteTerapeutaCompartido,
            PacienteTerapeutaCompartido.terapeutaid == Usuario.id,
        )
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente.id,
            PacienteTerapeutaCompartido.activo == True,
            Usuario.rol == 2,
            Usuario.activo == True,
            Usuario.consultorioid == consultorioid,
            or_(
                PacienteTerapeutaCompartido.fecha_inicio == None,
                PacienteTerapeutaCompartido.fecha_inicio <= hoy,
            ),
            or_(
                PacienteTerapeutaCompartido.fecha_fin == None,
                PacienteTerapeutaCompartido.fecha_fin >= hoy,
            ),
        )
        .order_by(Usuario.apellidos.asc(), Usuario.nombres.asc())
        .all()
    )

    for terapeuta in compartidos:
        _agregar_responsable_unico(
            responsables,
            ids,
            terapeuta,
            "Paciente compartido",
        )

    ahora = now_ecuador()
    permisos = (
        db.query(Usuario)
        .join(UsuarioPermisoTemporal, UsuarioPermisoTemporal.usuarioid == Usuario.id)
        .filter(
            Usuario.rol == 2,
            Usuario.activo == True,
            Usuario.consultorioid == consultorioid,
            UsuarioPermisoTemporal.activo == True,
            UsuarioPermisoTemporal.tipo_permiso == "atencion_sucursal_temporal",
            UsuarioPermisoTemporal.fecha_inicio <= ahora,
            UsuarioPermisoTemporal.fecha_fin >= ahora,
            or_(
                UsuarioPermisoTemporal.consultorioid == None,
                UsuarioPermisoTemporal.consultorioid == consultorioid,
            ),
        )
        .order_by(Usuario.apellidos.asc(), Usuario.nombres.asc())
        .all()
    )

    for terapeuta in permisos:
        _agregar_responsable_unico(
            responsables,
            ids,
            terapeuta,
            "Permiso temporal de atención",
        )

    return responsables


def _resolver_responsable_gimnasio(
    *,
    db: Session,
    paciente: Paciente,
    consultorioid: int | None,
    responsablegimnasioid: int | None,
    fecha_referencia: date | None = None,
) -> int:
    responsables = _responsables_gimnasio_autorizados(
        db=db,
        paciente=paciente,
        consultorioid=consultorioid,
        fecha_referencia=fecha_referencia,
    )

    responsables_ids = {usuario.id for usuario, _ in responsables}

    if responsablegimnasioid is not None:
        if responsablegimnasioid not in responsables_ids:
            raise HTTPException(
                status_code=400,
                detail=(
                    "El responsable de gimnasio seleccionado no puede atender "
                    "a este paciente en el consultorio donde se crea el gimnasio."
                ),
            )

        return responsablegimnasioid

    if len(responsables) == 1:
        return responsables[0][0].id

    if len(responsables) == 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "No hay fisioterapeuta autorizado para este paciente en el "
                "consultorio donde se quiere crear el gimnasio. Primero comparte "
                "o asigna el paciente a un fisioterapeuta de esa clínica."
            ),
        )

    raise HTTPException(
        status_code=400,
        detail=(
            "Este paciente tiene más de un fisioterapeuta autorizado para gimnasio. "
            "Seleccione el responsable de gimnasio."
        ),
    )


def _responsable_efectivo_membresia(membresia: MembresiaGimnasio, paciente: Paciente) -> int | None:
    return membresia.responsablegimnasioid or paciente.terapeutaasignadoid


def _validar_responsable_para_registro_gimnasio(
    *,
    membresia: MembresiaGimnasio,
    paciente: Paciente,
    current_user: Usuario,
    db: Session,
) -> None:
    if current_user.rol != 2:
        return

    responsable_id = _responsable_efectivo_membresia(membresia, paciente)

    if responsable_id is not None and responsable_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Esta membresía de gimnasio está asignada a otro fisioterapeuta.",
        )

    validar_acceso_paciente_por_rol(
        paciente=paciente,
        current_user=current_user,
        db=db,
    )

def _nombre_usuario(usuario: Usuario | None) -> str:
    if not usuario:
        return "Usuario"

    nombre = f"{usuario.nombres or ''} {usuario.apellidos or ''}".strip()

    if nombre:
        return nombre

    return usuario.email or f"Usuario {usuario.id}"


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


def _obtener_usuarios_actualizacion_gimnasio(
    db: Session,
    paciente: Paciente,
) -> list[Usuario]:
    usuarios: list[Usuario] = []
    usuarios_ids: set[int] = set()
    hoy = fecha_ecuador()

    if paciente.terapeutaasignadoid:
        terapeuta_principal = (
            db.query(Usuario)
            .filter(
                Usuario.id == paciente.terapeutaasignadoid,
                Usuario.rol == 2,
                Usuario.activo == True,
            )
            .first()
        )

        _agregar_usuario_unico(
            usuarios,
            usuarios_ids,
            terapeuta_principal,
        )

    terapeutas_compartidos = (
        db.query(Usuario)
        .join(
            PacienteTerapeutaCompartido,
            PacienteTerapeutaCompartido.terapeutaid == Usuario.id,
        )
        .filter(
            PacienteTerapeutaCompartido.pacienteid == paciente.id,
            PacienteTerapeutaCompartido.activo == True,
            Usuario.rol == 2,
            Usuario.activo == True,
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
    )

    for terapeuta in terapeutas_compartidos:
        _agregar_usuario_unico(
            usuarios,
            usuarios_ids,
            terapeuta,
        )

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
        _agregar_usuario_unico(
            usuarios,
            usuarios_ids,
            secretario,
        )

    jefes = (
        db.query(Usuario)
        .filter(
            Usuario.rol == 3,
            Usuario.activo == True,
        )
        .all()
    )

    for jefe in jefes:
        _agregar_usuario_unico(
            usuarios,
            usuarios_ids,
            jefe,
        )

    return usuarios


def _notificar_actualizacion_gimnasio(
    db: Session,
    paciente: Paciente,
    current_user: Usuario,
    tipo: str,
    titulo: str,
    mensaje: str,
    membresia: MembresiaGimnasio | None = None,
    movimiento: MovimientoGimnasio | None = None,
) -> None:
    usuarios_destino = _obtener_usuarios_actualizacion_gimnasio(
        db=db,
        paciente=paciente,
    )

    if not usuarios_destino:
        print("ℹ️ No hay usuarios destino para actualización de gimnasio.")
        return

    nombre_paciente = _nombre_paciente(paciente)
    creador = _nombre_usuario(current_user)

    referencia_id = None

    if membresia is not None:
        referencia_id = membresia.id
    elif movimiento is not None:
        referencia_id = movimiento.id

    notificaciones_creadas = 0

    for usuario in usuarios_destino:
        if usuario.id == current_user.id:
            continue

        crear_notificacion_usuario(
            db=db,
            usuarioid=usuario.id,
            titulo=titulo,
            mensaje=mensaje,
            tipo=tipo,
            referencia_tipo="gimnasio",
            referencia_id=referencia_id,
            data={
                "pacienteid": paciente.id,
                "paciente_id": paciente.id,
                "paciente_nombre": nombre_paciente,
                "consultorioid": paciente.consultorioid,
                "membresia_id": membresia.id if membresia else None,
                "modalidad": membresia.modalidad if membresia else None,
                "movimiento_id": movimiento.id if movimiento else None,
                "tipo_movimiento": movimiento.tipo if movimiento else None,
                "creado_por_id": current_user.id,
                "creado_por_nombre": creador,
                "actualizar": [
                    "gimnasio",
                    "pagos_gimnasio",
                    "dashboard",
                    "notificaciones",
                ],
            },
            hacer_flush=False,
        )

        notificaciones_creadas += 1

    db.flush()

    print(
        f"✅ Notificaciones de gimnasio creadas: "
        f"{notificaciones_creadas} para paciente {paciente.id}"
    )

def _obtener_membresia_activa(
    db: Session,
    paciente_id: int,
) -> Optional[MembresiaGimnasio]:
    return (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.pacienteid == paciente_id,
            MembresiaGimnasio.activo == True,
            MembresiaGimnasio.modalidad == MODALIDAD_MENSUAL,
        )
        .order_by(MembresiaGimnasio.fechainicio.desc())
        .first()
    )


def _calcular_resumen(
    db: Session,
    membresia: MembresiaGimnasio,
    fecha_referencia: Optional[date] = None,
) -> ResumenMembresiaGimnasioOut:
    hoy = fecha_referencia or fecha_ecuador()

    movimientos = (
        db.query(MovimientoGimnasio)
        .filter(MovimientoGimnasio.membresiaid == membresia.id)
        .all()
    )

    dias_asistidos = sum(
        1 for m in movimientos if m.tipo == TIPO_ASISTENCIA_GIMNASIO
    )

    dias_aplazados = sum(
        1 for m in movimientos if m.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO
    )

    total_dias_programados = int(membresia.diascontratados) + dias_aplazados

    fecha_fin_estimada = _sumar_dias_habiles_incluyendo_inicio(
        membresia.fechainicio,
        total_dias_programados,
    )

    fecha_limite_calculo = min(hoy, fecha_fin_estimada)

    dias_habiles_transcurridos = _contar_dias_habiles(
        membresia.fechainicio,
        fecha_limite_calculo,
    )

    # Los días aplazados por terapia no consumen cupo de gimnasio.
    dias_consumidos = max(
        dias_habiles_transcurridos - dias_aplazados,
        0,
    )

    dias_consumidos = min(
        dias_consumidos,
        int(membresia.diascontratados),
    )

    dias_restantes = max(
        int(membresia.diascontratados) - dias_consumidos,
        0,
    )

    dias_perdidos = max(
        dias_consumidos - dias_asistidos,
        0,
    )

    movimiento_hoy = next(
    (m for m in movimientos if m.fecha == hoy),
    None,
)

    puede_registrar_hoy = (
        membresia.activo
        and hoy >= membresia.fechainicio
        and _es_dia_habil(hoy)
        and dias_restantes > 0
        and hoy <= fecha_fin_estimada
        and movimiento_hoy is None
    )

    if not _es_dia_habil(hoy):
        mensaje = "Hoy no cuenta como día de gimnasio porque es fin de semana."
    elif hoy < membresia.fechainicio:
        mensaje = "La membresía todavía no inicia."
    elif dias_restantes <= 0:
        mensaje = "La membresía ya no tiene días disponibles."
    elif hoy > fecha_fin_estimada:
        mensaje = "La membresía ya finalizó."
    elif movimiento_hoy is not None:
        if movimiento_hoy.tipo == TIPO_ASISTENCIA_GIMNASIO:
            mensaje = "Ya se registró la asistencia de gimnasio de hoy."
        elif movimiento_hoy.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO:
            mensaje = "Hoy ya fue aplazado porque una terapia reemplazó el gimnasio."
        else:
            mensaje = "Ya existe un registro de gimnasio para hoy."
    else:
        mensaje = "La membresía está activa."

    return ResumenMembresiaGimnasioOut(
        membresia=membresia,
        fecha_fin_estimada=fecha_fin_estimada,
        dias_contratados=int(membresia.diascontratados),
        dias_habiles_transcurridos=dias_habiles_transcurridos,
        dias_asistidos=dias_asistidos,
        dias_aplazados_por_terapia=dias_aplazados,
        dias_perdidos=dias_perdidos,
        dias_consumidos=dias_consumidos,
        dias_restantes=dias_restantes,
        puede_registrar_hoy=puede_registrar_hoy,
        mensaje=mensaje,
    )


def _desactivar_membresia_mensual_si_terminada(
    db: Session,
    membresia: MembresiaGimnasio | None,
    fecha_referencia: Optional[date] = None,
) -> bool:
    """
    Devuelve True si la membresía mensual ya terminó y fue marcada como inactiva.

    Esto evita que una membresía con 0 días restantes, pero todavía con activo=True
    en base de datos, bloquee la renovación o el pase diario.
    """
    if not membresia:
        return False

    if membresia.modalidad != MODALIDAD_MENSUAL:
        return False

    if membresia.activo is not True:
        return False

    fecha_control = fecha_referencia or fecha_ecuador()

    resumen = _calcular_resumen(
        db=db,
        membresia=membresia,
        fecha_referencia=fecha_control,
    )

    membresia_terminada = (
        resumen.dias_restantes <= 0
        or fecha_control > resumen.fecha_fin_estimada
    )

    if not membresia_terminada:
        return False

    membresia.activo = False
    db.flush()
    return True



@router.get(
    "/paciente/{paciente_id}/responsables",
    response_model=List[GimnasioResponsableOut],
)
def listar_responsables_gimnasio_paciente(
    paciente_id: int,
    consultorioid: Optional[int] = Query(default=None),
    fecha: Optional[date] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    consultorio_operativo_id = _resolver_consultorio_operativo_gimnasio(
        paciente=paciente,
        current_user=current_user,
        consultorioid=consultorioid,
    )

    responsables = _responsables_gimnasio_autorizados(
        db=db,
        paciente=paciente,
        consultorioid=consultorio_operativo_id,
        fecha_referencia=fecha,
    )

    consultorio_nombre = _nombre_consultorio_por_id(db, consultorio_operativo_id)

    return [
        GimnasioResponsableOut(
            id=usuario.id,
            nombres=usuario.nombres or "",
            apellidos=usuario.apellidos or "",
            consultorioid=usuario.consultorioid,
            consultorio=consultorio_nombre,
            motivo=motivo,
        )
        for usuario, motivo in responsables
    ]



@router.post("/membresias", response_model=MembresiaGimnasioOut)
def crear_membresia_gimnasio(
    data: MembresiaGimnasioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=data.pacienteid,
        current_user=current_user,
    )

    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Solo jefe o secretario pueden crear membresías de gimnasio.",
        )

    if not _es_dia_habil(data.fechainicio):
        raise HTTPException(
            status_code=400,
            detail="La fecha de inicio debe ser de lunes a viernes.",
        )

    consultorio_operativo_id = _resolver_consultorio_operativo_gimnasio(
        paciente=paciente,
        current_user=current_user,
        consultorioid=data.consultorioid,
    )

    responsable_gimnasio_id = _resolver_responsable_gimnasio(
        db=db,
        paciente=paciente,
        consultorioid=consultorio_operativo_id,
        responsablegimnasioid=data.responsablegimnasioid,
        fecha_referencia=data.fechainicio,
    )

    membresia_activa = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente.id,
    )

    if membresia_activa:
        membresia_anterior_terminada = _desactivar_membresia_mensual_si_terminada(
            db=db,
            membresia=membresia_activa,
            fecha_referencia=data.fechainicio,
        )

        if not membresia_anterior_terminada:
            resumen_actual = _calcular_resumen(
                db=db,
                membresia=membresia_activa,
                fecha_referencia=data.fechainicio,
            )

            raise HTTPException(
                status_code=400,
                detail=(
                    "El paciente ya tiene una membresía de gimnasio activa. "
                    f"Le quedan {resumen_actual.dias_restantes} día(s). "
                    "Desactiva la membresía actual antes de crear otra."
                ),
            )

    nueva = MembresiaGimnasio(
        pacienteid=paciente.id,
        fechainicio=data.fechainicio,
        diascontratados=data.diascontratados,
        precio=data.precio,
        consultorioid=consultorio_operativo_id,
        responsablegimnasioid=responsable_gimnasio_id,
        modalidad=MODALIDAD_MENSUAL,
        activo=True,
        observaciones=data.observaciones,
    )

    db.add(nueva)
    db.flush()

    nombre_paciente = _nombre_paciente(paciente)

    _notificar_actualizacion_gimnasio(
        db=db,
        paciente=paciente,
        current_user=current_user,
        tipo="gimnasio_membresia_creada",
        titulo="Membresía de gimnasio creada",
        mensaje=(
            f"Se creó una membresía mensual de gimnasio para {nombre_paciente}. "
            "La información de gimnasio fue actualizada."
        ),
        membresia=nueva,
    )

    db.commit()
    db.refresh(nueva)

    return nueva

@router.post("/pases-diarios",response_model=PaseDiarioGimnasioOut,status_code=status.HTTP_201_CREATED,)
def registrar_pase_diario_gimnasio(
    data: PaseDiarioGimnasioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=data.pacienteid,
        current_user=current_user,
    )

    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Solo jefe o secretario pueden registrar pases diarios de gimnasio.",
        )

    fecha_pase = data.fecha or fecha_ecuador()

    consultorio_operativo_id = _resolver_consultorio_operativo_gimnasio(
        paciente=paciente,
        current_user=current_user,
        consultorioid=data.consultorioid,
    )

    responsable_gimnasio_id = _resolver_responsable_gimnasio(
        db=db,
        paciente=paciente,
        consultorioid=consultorio_operativo_id,
        responsablegimnasioid=data.responsablegimnasioid,
        fecha_referencia=fecha_pase,
    )

    if not _es_dia_habil(fecha_pase):
        raise HTTPException(
            status_code=400,
            detail="Solo se puede registrar gimnasio de lunes a viernes.",
        )

    membresia_mensual_activa = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente.id,
    )

    if membresia_mensual_activa:
        membresia_anterior_terminada = _desactivar_membresia_mensual_si_terminada(
            db=db,
            membresia=membresia_mensual_activa,
            fecha_referencia=fecha_pase,
        )

        if not membresia_anterior_terminada:
            resumen_actual = _calcular_resumen(
                db=db,
                membresia=membresia_mensual_activa,
                fecha_referencia=fecha_pase,
            )

            raise HTTPException(
                status_code=400,
                detail=(
                    "Este paciente ya tiene una membresía mensual activa. "
                    f"Le quedan {resumen_actual.dias_restantes} día(s). "
                    "Registra la asistencia desde la membresía, no como pase diario."
                ),
            )

    pase_existente = (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.pacienteid == paciente.id,
            MembresiaGimnasio.fechainicio == fecha_pase,
            MembresiaGimnasio.modalidad == MODALIDAD_DIARIA,
        )
        .first()
    )

    if pase_existente:
        raise HTTPException(
            status_code=400,
            detail="Este paciente ya tiene un pase diario registrado para esa fecha.",
        )

    movimiento_existente = (
        db.query(MovimientoGimnasio)
        .filter(
            MovimientoGimnasio.pacienteid == paciente.id,
            MovimientoGimnasio.fecha == fecha_pase,
            MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO,
        )
        .first()
    )

    if movimiento_existente:
        raise HTTPException(
            status_code=400,
            detail="Ya existe una asistencia de gimnasio registrada para ese paciente en esa fecha.",
        )

    # Importante: el pase diario NO crea pago automáticamente.
    # Algunas personas usan el gimnasio primero y pagan después.
    # El pago se registra luego desde /api/pagos/ o /api/pagos/registrar-con-comprobante
    # usando membresiagimnasioid = pase_diario.id.
    pase_diario = MembresiaGimnasio(
        pacienteid=paciente.id,
        fechainicio=fecha_pase,
        diascontratados=1,
        precio=data.precio,
        consultorioid=consultorio_operativo_id,
        responsablegimnasioid=responsable_gimnasio_id,
        modalidad=MODALIDAD_DIARIA,
        activo=False,
        observaciones=data.observacion,
    )

    db.add(pase_diario)
    db.flush()

    movimiento = MovimientoGimnasio(
        membresiaid=pase_diario.id,
        pacienteid=paciente.id,
        fecha=fecha_pase,
        tipo=TIPO_ASISTENCIA_GIMNASIO,
        sesionid=None,
        tratamientopacienteid=None,
        observacion=data.observacion or "Pase diario de gimnasio",
    )

    db.add(movimiento)
    db.flush()

    nombre_paciente = _nombre_paciente(paciente)

    _notificar_actualizacion_gimnasio(
        db=db,
        paciente=paciente,
        current_user=current_user,
        tipo="gimnasio_pase_diario_creado",
        titulo="Pase diario de gimnasio registrado",
        mensaje=(
            f"Se registró un pase diario de gimnasio para {nombre_paciente}. "
            "La información de gimnasio fue actualizada."
        ),
        membresia=pase_diario,
        movimiento=movimiento,
    )

    db.commit()

    db.refresh(pase_diario)
    db.refresh(movimiento)

    return PaseDiarioGimnasioOut(
        paciente=f"{paciente.nombres} {paciente.apellidos}",
        membresia=pase_diario,
        movimiento=movimiento,
        pago=None,
    )

@router.get(
    "/paciente/{paciente_id}/pases-diarios",
    response_model=List[PaseDiarioGimnasioOut],
)
def listar_pases_diarios_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    pases = (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.pacienteid == paciente_id,
            MembresiaGimnasio.modalidad == MODALIDAD_DIARIA,
        )
        .order_by(
            MembresiaGimnasio.fechainicio.desc(),
            MembresiaGimnasio.id.desc(),
        )
        .all()
    )

    if not pases:
        return []

    pase_ids = [pase.id for pase in pases]

    movimientos = (
        db.query(MovimientoGimnasio)
        .filter(
            MovimientoGimnasio.membresiaid.in_(pase_ids),
            MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO,
        )
        .order_by(MovimientoGimnasio.id.desc())
        .all()
    )

    ultimo_movimiento_por_membresia = {}

    for movimiento in movimientos:
        if movimiento.membresiaid not in ultimo_movimiento_por_membresia:
            ultimo_movimiento_por_membresia[movimiento.membresiaid] = movimiento

    pagos = (
        db.query(Pago)
        .filter(Pago.membresiagimnasioid.in_(pase_ids))
        .order_by(Pago.id.desc())
        .all()
    )

    ultimo_pago_por_membresia = {}

    for pago in pagos:
        if pago.membresiagimnasioid not in ultimo_pago_por_membresia:
            ultimo_pago_por_membresia[pago.membresiagimnasioid] = pago

    nombre_paciente = f"{paciente.nombres} {paciente.apellidos}"

    resultado = []

    for pase in pases:
        movimiento = ultimo_movimiento_por_membresia.get(pase.id)

        if not movimiento:
            continue

        resultado.append(
            PaseDiarioGimnasioOut(
                paciente=nombre_paciente,
                membresia=pase,
                movimiento=movimiento,
                pago=ultimo_pago_por_membresia.get(pase.id),
            )
        )

    return resultado

@router.get(
    "/pases-diarios",
    response_model=List[PaseDiarioGimnasioOut],
)
def listar_pases_diarios_gimnasio(
    fecha_desde: Optional[date] = Query(default=None),
    fecha_hasta: Optional[date] = Query(default=None),
    paciente_id: Optional[int] = Query(default=None),
    buscar: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(
            MembresiaGimnasio,
            MovimientoGimnasio,
            Paciente,
        )
        .join(
            MovimientoGimnasio,
            MovimientoGimnasio.membresiaid == MembresiaGimnasio.id,
        )
        .join(
            Paciente,
            Paciente.id == MembresiaGimnasio.pacienteid,
        )
        .filter(
            MembresiaGimnasio.modalidad == MODALIDAD_DIARIA,
            MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO,
        )
    )

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        query = query.filter(
            func.coalesce(MembresiaGimnasio.consultorioid, Paciente.consultorioid)
            == current_user.consultorioid
        )

    elif current_user.rol == 2:
        query = query.filter(
            func.coalesce(
                MembresiaGimnasio.responsablegimnasioid,
                Paciente.terapeutaasignadoid,
            )
            == current_user.id
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado.",
        )

    if paciente_id is not None:
        query = query.filter(Paciente.id == paciente_id)

    if fecha_desde is not None:
        query = query.filter(MovimientoGimnasio.fecha >= fecha_desde)

    if fecha_hasta is not None:
        query = query.filter(MovimientoGimnasio.fecha <= fecha_hasta)

    if buscar and buscar.strip():
        texto = f"%{buscar.strip()}%"

        query = query.filter(
            or_(
                Paciente.nombres.ilike(texto),
                Paciente.apellidos.ilike(texto),
                MovimientoGimnasio.observacion.ilike(texto),
            )
        )

    filas = (
        query.order_by(
            MovimientoGimnasio.fecha.desc(),
            MembresiaGimnasio.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not filas:
        return []

    membresia_ids = [membresia.id for membresia, _, _ in filas]

    pagos = (
        db.query(Pago)
        .filter(Pago.membresiagimnasioid.in_(membresia_ids))
        .order_by(Pago.id.desc())
        .all()
    )

    ultimo_pago_por_membresia = {}

    for pago in pagos:
        if pago.membresiagimnasioid not in ultimo_pago_por_membresia:
            ultimo_pago_por_membresia[pago.membresiagimnasioid] = pago

    resultado = []

    for membresia, movimiento, paciente in filas:
        resultado.append(
            PaseDiarioGimnasioOut(
                paciente=f"{paciente.nombres} {paciente.apellidos}",
                membresia=membresia,
                movimiento=movimiento,
                pago=ultimo_pago_por_membresia.get(membresia.id),
            )
        )

    return resultado



def _aplicar_filtro_acceso_gimnasio(query, current_user: Usuario):
    """Aplica el alcance de la pantalla rápida sin cargar objetos completos.

    Regla de negocio: la asistencia rápida de gimnasio solo la registran
    los terapeutas. El listado se limita a sus pacientes asignados.
    """
    if current_user.rol != 2:
        raise HTTPException(
            status_code=403,
            detail="Solo los terapeutas pueden registrar asistencia de gimnasio.",
        )

    return query.filter(
        func.coalesce(
            MembresiaGimnasio.responsablegimnasioid,
            Paciente.terapeutaasignadoid,
        )
        == current_user.id
    )


def _mensaje_asistencia_rapida(
    *,
    hoy: date,
    fechainicio: date,
    fecha_fin_estimada: date,
    dias_restantes: int,
    asistencia_hoy_registrada: bool,
) -> tuple[bool, str]:
    if not _es_dia_habil(hoy):
        return False, "Hoy no cuenta como día de gimnasio porque es fin de semana."

    if hoy < fechainicio:
        return False, "La membresía todavía no inicia."

    if asistencia_hoy_registrada:
        return False, "Ya se registró la asistencia de gimnasio de hoy."

    if dias_restantes <= 0:
        return False, "La membresía ya no tiene días disponibles."

    if hoy > fecha_fin_estimada:
        return False, "La membresía ya finalizó."

    return True, "Lista para registrar asistencia de hoy."


def _armar_asistencia_rapida_out(
    *,
    membresia: MembresiaGimnasio,
    paciente: Paciente,
    hoy: date,
    dias_asistidos: int,
    dias_aplazados: int,
    asistencia_hoy_registrada: bool,
    ultima_asistencia: date | None,
) -> GimnasioAsistenciaRapidaOut:
    dias_contratados = int(membresia.diascontratados or 0)
    total_dias_programados = dias_contratados + dias_aplazados

    fecha_fin_estimada = _sumar_dias_habiles_incluyendo_inicio(
        membresia.fechainicio,
        total_dias_programados,
    )

    if hoy < membresia.fechainicio:
        dias_habiles_transcurridos = 0
    else:
        dias_habiles_transcurridos = _contar_dias_habiles(
            membresia.fechainicio,
            min(hoy, fecha_fin_estimada),
        )

    # Los días aplazados por terapia no consumen cupo de gimnasio.
    dias_consumidos = max(dias_habiles_transcurridos - dias_aplazados, 0)
    dias_consumidos = min(dias_consumidos, dias_contratados)
    dias_restantes = max(dias_contratados - dias_consumidos, 0)

    puede_registrar, mensaje = _mensaje_asistencia_rapida(
        hoy=hoy,
        fechainicio=membresia.fechainicio,
        fecha_fin_estimada=fecha_fin_estimada,
        dias_restantes=dias_restantes,
        asistencia_hoy_registrada=asistencia_hoy_registrada,
    )

    nombre_paciente = f"{paciente.nombres or ''} {paciente.apellidos or ''}".strip()

    return GimnasioAsistenciaRapidaOut(
        pacienteid=paciente.id,
        paciente=nombre_paciente or "Paciente",
        cedula=paciente.cedula,
        consultorioid=paciente.consultorioid,
        membresiaid=membresia.id,
        fechainicio=membresia.fechainicio,
        fecha_fin_estimada=fecha_fin_estimada,
        dias_contratados=dias_contratados,
        dias_asistidos=dias_asistidos,
        dias_aplazados_por_terapia=dias_aplazados,
        dias_consumidos=dias_consumidos,
        dias_restantes=dias_restantes,
        ultima_asistencia=ultima_asistencia,
        asistencia_hoy_registrada=asistencia_hoy_registrada,
        puede_registrar_hoy=puede_registrar,
        mensaje=mensaje,
    )


@router.get(
    "/asistencias-rapidas",
    response_model=List[GimnasioAsistenciaRapidaOut],
)
def listar_asistencias_rapidas_gimnasio(
    buscar: Optional[str] = Query(default=None),
    limit: int = Query(default=30, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """Lista pacientes con membresía mensual activa para registrar asistencia.

    Optimización: trae máximo 50 filas y calcula los movimientos con una sola
    agregación SQL, sin abrir reportes ni pagos.
    """
    hoy = fecha_ecuador()

    query = (
        db.query(MembresiaGimnasio, Paciente)
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
        .filter(
            MembresiaGimnasio.activo == True,
            MembresiaGimnasio.modalidad == MODALIDAD_MENSUAL,
        )
    )

    query = _aplicar_filtro_acceso_gimnasio(query, current_user)

    if buscar and buscar.strip():
        texto = f"%{buscar.strip()}%"
        query = query.filter(
            or_(
                Paciente.nombres.ilike(texto),
                Paciente.apellidos.ilike(texto),
                Paciente.cedula.ilike(texto),
                func.concat(Paciente.nombres, " ", Paciente.apellidos).ilike(texto),
                func.concat(Paciente.apellidos, " ", Paciente.nombres).ilike(texto),
            )
        )

    filas = (
        query.order_by(Paciente.apellidos.asc(), Paciente.nombres.asc(), MembresiaGimnasio.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not filas:
        return []

    membresia_ids = [membresia.id for membresia, _ in filas]

    agregados = (
        db.query(
            MovimientoGimnasio.membresiaid.label("membresiaid"),
            func.coalesce(
                func.sum(
                    case(
                        (MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("dias_asistidos"),
            func.coalesce(
                func.sum(
                    case(
                        (MovimientoGimnasio.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("dias_aplazados"),
            func.coalesce(
                func.max(
                    case(
                        (MovimientoGimnasio.fecha == hoy, 1),
                        else_=0,
                    )
                ),
                0,
            ).label("tiene_hoy"),
            func.max(
                case(
                    (MovimientoGimnasio.tipo == TIPO_ASISTENCIA_GIMNASIO, MovimientoGimnasio.fecha),
                    else_=None,
                )
            ).label("ultima_asistencia"),
        )
        .filter(MovimientoGimnasio.membresiaid.in_(membresia_ids))
        .group_by(MovimientoGimnasio.membresiaid)
        .all()
    )

    agregados_por_membresia = {
        row.membresiaid: {
            "dias_asistidos": int(row.dias_asistidos or 0),
            "dias_aplazados": int(row.dias_aplazados or 0),
            "tiene_hoy": int(row.tiene_hoy or 0) > 0,
            "ultima_asistencia": row.ultima_asistencia,
        }
        for row in agregados
    }

    resultado: list[GimnasioAsistenciaRapidaOut] = []

    for membresia, paciente in filas:
        datos = agregados_por_membresia.get(membresia.id, {})

        resultado.append(
            _armar_asistencia_rapida_out(
                membresia=membresia,
                paciente=paciente,
                hoy=hoy,
                dias_asistidos=int(datos.get("dias_asistidos", 0) or 0),
                dias_aplazados=int(datos.get("dias_aplazados", 0) or 0),
                asistencia_hoy_registrada=bool(datos.get("tiene_hoy", False)),
                ultima_asistencia=datos.get("ultima_asistencia"),
            )
        )

    return resultado


@router.post(
    "/asistencias-rapidas/{membresia_id}/registrar",
    response_model=MovimientoGimnasioOut,
    status_code=status.HTTP_201_CREATED,
)
def registrar_asistencia_rapida_gimnasio(
    membresia_id: int,
    data: GimnasioAsistenciaRapidaCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """Registra asistencia de gimnasio desde el dashboard con datos mínimos."""
    fila = (
        db.query(MembresiaGimnasio, Paciente)
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not fila:
        raise HTTPException(status_code=404, detail="Membresía no encontrada.")

    membresia, paciente = fila

    if current_user.rol != 2:
        raise HTTPException(
            status_code=403,
            detail="Solo los terapeutas pueden registrar asistencia de gimnasio.",
        )

    _validar_responsable_para_registro_gimnasio(
        membresia=membresia,
        paciente=paciente,
        current_user=current_user,
        db=db,
    )

    if membresia.modalidad != MODALIDAD_MENSUAL or membresia.activo is not True:
        raise HTTPException(
            status_code=400,
            detail="La membresía no está activa para registro rápido.",
        )

    fecha_movimiento = data.fecha or fecha_ecuador()

    if not _es_dia_habil(fecha_movimiento):
        raise HTTPException(
            status_code=400,
            detail="Solo se puede registrar gimnasio de lunes a viernes.",
        )

    if _desactivar_membresia_mensual_si_terminada(
        db=db,
        membresia=membresia,
        fecha_referencia=fecha_movimiento,
    ):
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "La membresía ya no tiene días disponibles. "
                "Crea una nueva membresía para registrar más asistencias."
            ),
        )

    resumen = _calcular_resumen(
        db=db,
        membresia=membresia,
        fecha_referencia=fecha_movimiento,
    )

    if not resumen.puede_registrar_hoy:
        raise HTTPException(status_code=400, detail=resumen.mensaje)

    movimiento = MovimientoGimnasio(
        membresiaid=membresia.id,
        pacienteid=paciente.id,
        fecha=fecha_movimiento,
        tipo=TIPO_ASISTENCIA_GIMNASIO,
        sesionid=None,
        tratamientopacienteid=None,
        observacion=(data.observacion or "Asistencia registrada desde dashboard"),
    )

    db.add(movimiento)
    db.flush()

    nombre_paciente = _nombre_paciente(paciente)

    _notificar_actualizacion_gimnasio(
        db=db,
        paciente=paciente,
        current_user=current_user,
        tipo="gimnasio_asistencia_rapida_registrada",
        titulo="Asistencia de gimnasio registrada",
        mensaje=(
            f"Se registró una asistencia de gimnasio para {nombre_paciente}. "
            "La membresía fue actualizada."
        ),
        membresia=membresia,
        movimiento=movimiento,
    )

    db.commit()
    db.refresh(movimiento)

    return movimiento


@router.get(
    "/paciente/{paciente_id}/activa",
    response_model=Optional[ResumenMembresiaGimnasioOut],
)
def obtener_membresia_activa_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_acceso_paciente(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    membresia = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente_id,
    )

    if not membresia:
        return None

    return _calcular_resumen(
        db=db,
        membresia=membresia,
    )


@router.get(
    "/membresias/{membresia_id}/resumen",
    response_model=ResumenMembresiaGimnasioOut,
)
def obtener_resumen_membresia(
    membresia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    membresia = (
        db.query(MembresiaGimnasio)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=404,
            detail="Membresía no encontrada.",
        )

    _validar_acceso_paciente(
        db=db,
        paciente_id=membresia.pacienteid,
        current_user=current_user,
    )

    return _calcular_resumen(
        db=db,
        membresia=membresia,
    )


@router.get(
    "/membresias/{membresia_id}/movimientos",
    response_model=List[MovimientoGimnasioOut],
)
def listar_movimientos_membresia(
    membresia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    membresia = (
        db.query(MembresiaGimnasio)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=404,
            detail="Membresía no encontrada.",
        )

    _validar_acceso_paciente(
        db=db,
        paciente_id=membresia.pacienteid,
        current_user=current_user,
    )

    return (
        db.query(MovimientoGimnasio)
        .filter(MovimientoGimnasio.membresiaid == membresia.id)
        .order_by(MovimientoGimnasio.fecha.desc())
        .all()
    )


@router.post("/movimientos", response_model=MovimientoGimnasioOut)
def registrar_movimiento_gimnasio(
    data: MovimientoGimnasioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_acceso_paciente(
        db=db,
        paciente_id=data.pacienteid,
        current_user=current_user,
    )

    fecha_movimiento = data.fecha or fecha_ecuador()

    if not _es_dia_habil(fecha_movimiento):
        raise HTTPException(
            status_code=400,
            detail="Solo se puede registrar gimnasio de lunes a viernes.",
        )

    membresia = _obtener_membresia_activa(
        db=db,
        paciente_id=paciente.id,
    )

    if not membresia:
        raise HTTPException(
            status_code=400,
            detail="El paciente no tiene una membresía de gimnasio activa.",
        )

    _validar_responsable_para_registro_gimnasio(
        membresia=membresia,
        paciente=paciente,
        current_user=current_user,
        db=db,
    )

    if _desactivar_membresia_mensual_si_terminada(
        db=db,
        membresia=membresia,
        fecha_referencia=fecha_movimiento,
    ):
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=(
                "La membresía ya no tiene días disponibles. "
                "Crea una nueva membresía para registrar más asistencias."
            ),
        )
    
    if fecha_movimiento < membresia.fechainicio:
        raise HTTPException(
            status_code=400,
            detail="No se puede registrar gimnasio antes de la fecha de inicio de la membresía.",
        )

    resumen = _calcular_resumen(
        db=db,
        membresia=membresia,
        fecha_referencia=fecha_movimiento,
    )

    if resumen.dias_restantes <= 0:
        raise HTTPException(
            status_code=400,
            detail="La membresía ya no tiene días disponibles.",
        )

    existente = (
        db.query(MovimientoGimnasio)
        .filter(
            MovimientoGimnasio.membresiaid == membresia.id,
            MovimientoGimnasio.fecha == fecha_movimiento,
        )
        .first()
    )

    if existente:
        raise HTTPException(
            status_code=400,
            detail="Ya existe un movimiento de gimnasio registrado para esta fecha.",
        )

    if data.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO:
        if not data.sesionid:
            raise HTTPException(
                status_code=400,
                detail="Debe enviar la sesión que reemplazó el día de gimnasio.",
            )

        sesion = (
            db.query(SesionTerapia)
            .filter(SesionTerapia.id == data.sesionid)
            .first()
        )

        if not sesion:
            raise HTTPException(
                status_code=404,
                detail="Sesión no encontrada.",
            )

        if sesion.pacienteid != paciente.id:
            raise HTTPException(
                status_code=400,
                detail="La sesión no pertenece al paciente de la membresía.",
            )

    movimiento = MovimientoGimnasio(
        membresiaid=membresia.id,
        pacienteid=paciente.id,
        fecha=fecha_movimiento,
        tipo=data.tipo,
        sesionid=data.sesionid,
        tratamientopacienteid=data.tratamientopacienteid,
        observacion=data.observacion,
    )

    db.add(movimiento)
    db.flush()

    nombre_paciente = _nombre_paciente(paciente)

    if movimiento.tipo == TIPO_ASISTENCIA_GIMNASIO:
        titulo = "Asistencia de gimnasio registrada"
        mensaje = (
            f"Se registró una asistencia de gimnasio para {nombre_paciente}. "
            "La membresía fue actualizada."
        )
    elif movimiento.tipo == TIPO_TERAPIA_REEMPLAZA_GIMNASIO:
        titulo = "Día de gimnasio aplazado por terapia"
        mensaje = (
            f"Se aplazó un día de gimnasio de {nombre_paciente} por terapia. "
            "La membresía fue actualizada."
        )
    else:
        titulo = "Gimnasio actualizado"
        mensaje = (
            f"Se registró un movimiento de gimnasio para {nombre_paciente}. "
            "La membresía fue actualizada."
        )

    _notificar_actualizacion_gimnasio(
        db=db,
        paciente=paciente,
        current_user=current_user,
        tipo="gimnasio_movimiento_registrado",
        titulo=titulo,
        mensaje=mensaje,
        membresia=membresia,
        movimiento=movimiento,
    )

    db.commit()
    db.refresh(movimiento)

    return movimiento


@router.put("/membresias/{membresia_id}/desactivar", response_model=MembresiaGimnasioOut)
def desactivar_membresia_gimnasio(
    membresia_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    membresia = (
        db.query(MembresiaGimnasio)
        .filter(MembresiaGimnasio.id == membresia_id)
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=404,
            detail="Membresía no encontrada.",
        )

    _validar_acceso_paciente(
        db=db,
        paciente_id=membresia.pacienteid,
        current_user=current_user,
    )

    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Solo jefe o secretario pueden desactivar membresías.",
        )

    membresia.activo = False

    paciente = db.query(Paciente).filter(Paciente.id == membresia.pacienteid).first()

    if paciente:
        nombre_paciente = _nombre_paciente(paciente)

        _notificar_actualizacion_gimnasio(
            db=db,
            paciente=paciente,
            current_user=current_user,
            tipo="gimnasio_membresia_desactivada",
            titulo="Membresía de gimnasio desactivada",
            mensaje=(
                f"Se desactivó la membresía de gimnasio de {nombre_paciente}. "
                "La información de gimnasio fue actualizada."
            ),
            membresia=membresia,
        )

    db.commit()
    db.refresh(membresia)

    return membresia