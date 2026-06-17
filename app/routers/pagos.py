from collections import defaultdict
from datetime import date
import json
from types import SimpleNamespace
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status, Query
from sqlalchemy import Date, and_, case, cast, exists, func, or_
from sqlalchemy.orm import Session, aliased, joinedload

from app.schemas.pago import (
    CuentaMembresiaGimnasioOut,
    CuentaPaqueteOut,
    CuentaTratamientoOut,
    CuentaEcuasanitasOut,
    PagoCreate,
    PagoPrevioTratamientoCreate,
    PagoPrevioGimnasioCreate,
    RecuperacionCarteraCreate,
    PagoOut,
    PagoSimpleOut,
    PagoAnularRequest,
    SesionPendienteTerapeutaOut,
    PacienteDeudaTerapeutaOut,
    ResumenDeudasTerapeutaOut,
)

from ..auth.dependencies import get_current_secretary, get_current_user
from ..auth.permissions import (
    validar_acceso_paciente_por_rol,
    validar_consultorio_secretario,
)
from ..dependencies.db import get_db
from ..models.gimnasio import MembresiaGimnasio
from ..models.paciente import Paciente
from ..models.paciente_paquete import PacientePaquete
from ..models.pago import Pago
from ..models.paquete import Paquete
from ..models.sesion_terapia import SesionTerapia
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..services.notificacion_service import crear_notificacion_usuario
from ..utils.fechas import now_utc
from ..services.supabase_storage import (
    crear_url_firmada_comprobante,
    subir_comprobante_pago,
)

router = APIRouter(prefix="/api/pagos", tags=["pagos"])

# Terapias: 35% fisioterapeuta / 65% clínica.
# Ecuasanitas solo aplica a terapias, no a gimnasio.
PORCENTAJE_FISIO_TERAPIA = 0.35
PORCENTAJE_CLINICA_TERAPIA = 0.65


# ============================================================
# HELPERS
# ============================================================

def _es_transferencia(metodo: str) -> bool:
    return "transfer" in (metodo or "").strip().lower()


def _estado_pago_por_metodo(metodo: str) -> int:
    if _es_transferencia(metodo):
        return 1  # Pendiente
    return 2  # Verificado


def _fecha_caja_pago_expr():
    return func.coalesce(
        Pago.fechapagoreal,
        cast(func.timezone("America/Guayaquil", Pago.fechapago), Date),
    )


def _estado_cuenta(total_generado: float, pagado: float, saldo: float) -> str:
    if total_generado <= 0 and pagado <= 0:
        return "SIN CARGOS"

    if pagado <= 0:
        return "PENDIENTE"

    if saldo > 0:
        return "PARCIAL"

    return "PAGADO"


def _condicion_paciente_ecuasanitas():
    """
    Pacientes cuyo seguro/convenio cubre terapias por Ecuasanitas.

    Esta condición solo aplica a terapias; gimnasio mensual y diario se
    cobran normal aunque el paciente tenga Ecuasanitas.
    """
    return or_(
        Paciente.esecuasanitas == True,
        Paciente.tiposeguro.ilike("%ecuasanitas%"),
    )


def _sesion_finalizada_tratamiento_en_consultorio_exists(consultorioid: int):
    """
    Condición SQL optimizada para pacientes compartidos.

    Un tratamiento es visible para una sede si:
    - el paciente pertenece a esa sede, o
    - el tratamiento ya tiene una sesión finalizada atendida por un terapeuta
      de esa sede.
    """
    terapeuta_sesion = aliased(Usuario)

    return exists().where(
        and_(
            SesionTerapia.tratamientopacienteid == TratamientoPaciente.id,
            SesionTerapia.terapeutaid == terapeuta_sesion.id,
            terapeuta_sesion.consultorioid == consultorioid,
            SesionTerapia.horasalida != None,
        )
    )


def _tratamiento_visible_para_consultorio_filter(consultorioid: int):
    return or_(
        Paciente.consultorioid == consultorioid,
        _sesion_finalizada_tratamiento_en_consultorio_exists(consultorioid),
    )


def _tratamiento_visible_para_terapeuta_filter(terapeutaid: int):
    return or_(
        Paciente.terapeutaasignadoid == terapeutaid,
        exists().where(
            and_(
                SesionTerapia.tratamientopacienteid == TratamientoPaciente.id,
                SesionTerapia.terapeutaid == terapeutaid,
                SesionTerapia.horasalida != None,
            )
        ),
    )



def _tratamiento_con_sesion_de_terapeuta_exists(terapeutaid: int):
    return exists().where(
        and_(
            SesionTerapia.tratamientopacienteid == TratamientoPaciente.id,
            SesionTerapia.terapeutaid == terapeutaid,
            SesionTerapia.horasalida != None,
        )
    )


def _obtener_terapeuta_para_filtro(
    db: Session,
    terapeuta_sesion_id: Optional[int],
    current_user: Usuario,
    consultorio_operativo_id: Optional[int],
) -> Optional[Usuario]:
    # Valida el filtro por fisioterapeuta que PRACTICO la sesion.
    # No usa el terapeuta asignado del paciente; usa SesionTerapia.terapeutaid.
    if terapeuta_sesion_id is None:
        return None

    terapeuta = (
        db.query(Usuario)
        .filter(
            Usuario.id == terapeuta_sesion_id,
            Usuario.rol == 2,
            Usuario.activo == True,
        )
        .first()
    )

    if not terapeuta:
        raise HTTPException(
            status_code=404,
            detail="Fisioterapeuta no encontrado o inactivo.",
        )

    if current_user.rol == 2 and terapeuta.id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Solo puedes filtrar tus propias sesiones.",
        )

    if consultorio_operativo_id is not None and terapeuta.consultorioid != consultorio_operativo_id:
        raise HTTPException(
            status_code=403,
            detail="El fisioterapeuta no pertenece a la clínica seleccionada.",
        )

    return terapeuta


def _calcular_cobertura_fifo_por_terapeuta(
    db: Session,
    tratamiento_ids: List[int],
    terapeutaid: int,
):
    # Calcula generado/cubierto/pendiente para sesiones realizadas por un fisio.
    # Los pagos solo cubren el mismo tratamientopacienteid y se consumen FIFO.
    if not tratamiento_ids:
        return {}

    sesiones = (
        db.query(
            SesionTerapia.id,
            SesionTerapia.tratamientopacienteid,
            SesionTerapia.terapeutaid,
            SesionTerapia.fecha,
            SesionTerapia.horaingreso,
            TratamientoPaciente.precio_sesion_aplicado,
        )
        .join(
            TratamientoPaciente,
            TratamientoPaciente.id == SesionTerapia.tratamientopacienteid,
        )
        .filter(
            SesionTerapia.tratamientopacienteid.in_(tratamiento_ids),
            SesionTerapia.horasalida != None,
        )
        .order_by(
            SesionTerapia.tratamientopacienteid.asc(),
            SesionTerapia.fecha.asc(),
            SesionTerapia.horaingreso.asc(),
            SesionTerapia.id.asc(),
        )
        .all()
    )

    pagos_rows = (
        db.query(Pago)
        .filter(
            Pago.tratamientopacienteid.in_(tratamiento_ids),
            Pago.estadopago == 2,
            or_(Pago.anulado == False, Pago.anulado.is_(None)),
            or_(
                Pago.esrecuperacioncartera == False,
                Pago.esrecuperacioncartera.is_(None),
            ),
        )
        .order_by(
            Pago.tratamientopacienteid.asc(),
            Pago.fechapago.asc(),
            Pago.id.asc(),
        )
        .all()
    )

    pagos_por_tratamiento = defaultdict(list)
    for pago in pagos_rows:
        pagos_por_tratamiento[pago.tratamientopacienteid].append(
            {
                "restante": float(pago.monto or 0),
                "es_previo": bool(getattr(pago, "espagoprevio", False)),
            }
        )

    resultado = defaultdict(
        lambda: {
            "sesiones_realizadas": 0,
            "total_generado": 0.0,
            "pagado_caja_verificado": 0.0,
            "pago_previo_verificado": 0.0,
            "pagado_verificado": 0.0,
            "saldo": 0.0,
            "saldo_favor": 0.0,
        }
    )

    for sesion in sesiones:
        tratamiento_id = sesion.tratamientopacienteid
        precio = float(sesion.precio_sesion_aplicado or 0)
        restante_sesion = precio
        cubierto_caja = 0.0
        cubierto_previo = 0.0

        for pago in pagos_por_tratamiento.get(tratamiento_id, []):
            if restante_sesion <= 0:
                break
            if pago["restante"] <= 0:
                continue

            aplicado = min(restante_sesion, pago["restante"])
            pago["restante"] -= aplicado
            restante_sesion -= aplicado

            if pago["es_previo"]:
                cubierto_previo += aplicado
            else:
                cubierto_caja += aplicado

        if sesion.terapeutaid != terapeutaid:
            continue

        item = resultado[tratamiento_id]
        item["sesiones_realizadas"] += 1
        item["total_generado"] += precio
        item["pagado_caja_verificado"] += cubierto_caja
        item["pago_previo_verificado"] += cubierto_previo
        item["pagado_verificado"] += cubierto_caja + cubierto_previo
        item["saldo"] += max(restante_sesion, 0)

    return {
        tratamiento_id: {
            key: round(value, 2) if isinstance(value, float) else value
            for key, value in data.items()
        }
        for tratamiento_id, data in resultado.items()
    }


def _pago_visible_para_consultorio_filter(consultorioid: int):
    """
    Para listar/verificar pagos de pacientes compartidos.

    Incluye pagos del paciente de la sede y pagos aplicados a tratamientos
    que tienen atención realizada por terapeutas de la sede.
    """
    terapeuta_sesion = aliased(Usuario)

    return or_(
        Paciente.consultorioid == consultorioid,
        exists().where(
            and_(
                SesionTerapia.tratamientopacienteid == Pago.tratamientopacienteid,
                SesionTerapia.terapeutaid == terapeuta_sesion.id,
                terapeuta_sesion.consultorioid == consultorioid,
                SesionTerapia.horasalida != None,
            )
        ),
    )


def _paciente_tiene_atencion_en_consultorio(
    db: Session,
    pacienteid: int,
    consultorioid: Optional[int],
    tratamientopacienteid: Optional[int] = None,
) -> bool:
    if consultorioid is None:
        return False

    query = (
        db.query(SesionTerapia.id)
        .join(Usuario, Usuario.id == SesionTerapia.terapeutaid)
        .filter(
            SesionTerapia.pacienteid == pacienteid,
            Usuario.consultorioid == consultorioid,
            SesionTerapia.horasalida != None,
        )
    )

    if tratamientopacienteid is not None:
        query = query.filter(
            SesionTerapia.tratamientopacienteid == tratamientopacienteid
        )

    return query.first() is not None


def _validar_paciente(
    db: Session,
    pacienteid: int,
    current_user: Usuario,
    tratamientopacienteid: Optional[int] = None,
    permitir_atencion_compartida: bool = False,
) -> Paciente:
    paciente = db.query(Paciente).filter(Paciente.id == pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    try:
        validar_acceso_paciente_por_rol(paciente, current_user)
        return paciente
    except HTTPException as exc:
        # Pacientes compartidos:
        # El paciente puede pertenecer al Centro, pero si ya fue atendido por
        # un terapeuta de Atahualpa, la secretaria de Atahualpa debe poder
        # cobrar/verificar ese tratamiento.
        if (
            permitir_atencion_compartida
            and current_user.rol == 1
            and current_user.consultorioid is not None
            and _paciente_tiene_atencion_en_consultorio(
                db=db,
                pacienteid=pacienteid,
                consultorioid=current_user.consultorioid,
                tratamientopacienteid=tratamientopacienteid,
            )
        ):
            return paciente

        raise exc


def _obtener_pago_con_acceso(
    db: Session,
    pago_id: int,
    current_user: Usuario,
) -> tuple[Pago, Paciente]:
    """
    Devuelve (pago, paciente) para evitar que los callers tengan que
    volver a consultar el paciente.
    """
    pago = db.query(Pago).filter(Pago.id == pago_id).first()

    if not pago:
        raise HTTPException(
            status_code=404,
            detail="Pago no encontrado",
        )

    paciente = db.query(Paciente).filter(Paciente.id == pago.pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente del pago no encontrado",
        )

    _validar_paciente(
        db=db,
        pacienteid=paciente.id,
        current_user=current_user,
        tratamientopacienteid=pago.tratamientopacienteid,
        permitir_atencion_compartida=True,
    )

    return pago, paciente


def _validar_paciente_paquete(
    db: Session,
    pacienteid: int,
    pacientepaqueteid: Optional[int],
) -> Optional[PacientePaquete]:
    if pacientepaqueteid is None:
        return None

    paciente_paquete = (
        db.query(PacientePaquete)
        .filter(
            PacientePaquete.id == pacientepaqueteid,
            PacientePaquete.pacienteid == pacienteid,
        )
        .first()
    )

    if not paciente_paquete:
        raise HTTPException(
            status_code=400,
            detail="El paquete asignado no existe o no pertenece al paciente.",
        )

    return paciente_paquete


def _validar_tratamiento_paciente(
    db: Session,
    pacienteid: int,
    tratamientopacienteid: Optional[int],
) -> Optional[TratamientoPaciente]:
    if tratamientopacienteid is None:
        return None

    tratamiento = (
        db.query(TratamientoPaciente)
        .filter(
            TratamientoPaciente.id == tratamientopacienteid,
            TratamientoPaciente.pacienteid == pacienteid,
        )
        .first()
    )

    if not tratamiento:
        raise HTTPException(
            status_code=400,
            detail="El tratamiento no existe o no pertenece al paciente.",
        )

    return tratamiento


def _validar_membresia_gimnasio(
    db: Session,
    pacienteid: int,
    membresiagimnasioid: Optional[int],
) -> Optional[MembresiaGimnasio]:
    if membresiagimnasioid is None:
        return None

    membresia = (
        db.query(MembresiaGimnasio)
        .filter(
            MembresiaGimnasio.id == membresiagimnasioid,
            MembresiaGimnasio.pacienteid == pacienteid,
        )
        .first()
    )

    if not membresia:
        raise HTTPException(
            status_code=400,
            detail="La membresía de gimnasio no existe o no pertenece al paciente.",
        )

    return membresia


def _validar_destino_pago(
    pacientepaqueteid: Optional[int],
    tratamientopacienteid: Optional[int],
    membresiagimnasioid: Optional[int],
):
    destinos = [
        pacientepaqueteid is not None,
        tratamientopacienteid is not None,
        membresiagimnasioid is not None,
    ]

    if sum(destinos) == 0:
        raise HTTPException(
            status_code=400,
            detail="Debe seleccionar paquete, tratamiento o membresía de gimnasio para registrar el pago.",
        )

    if sum(destinos) > 1:
        raise HTTPException(
            status_code=400,
            detail="El pago solo puede pertenecer a un paquete, tratamiento o membresía de gimnasio.",
        )


def _validar_saldo_paquete(
    db: Session,
    paciente_paquete: Optional[PacientePaquete],
    monto: float,
    excluir_pago_id: Optional[int] = None,
):
    if paciente_paquete is None:
        return

    query = db.query(
        func.coalesce(
            func.sum(
                case((Pago.estadopago == 2, Pago.monto), else_=0)
            ),
            0,
        ).label("verificado"),
        func.coalesce(
            func.sum(
                case((Pago.estadopago == 1, Pago.monto), else_=0)
            ),
            0,
        ).label("pendiente"),
    ).filter(
        Pago.pacientepaqueteid == paciente_paquete.id,
        Pago.anulado == False,
    )

    if excluir_pago_id is not None:
        query = query.filter(Pago.id != excluir_pago_id)

    totales = query.one()

    precio_final = float(paciente_paquete.preciofinal)

    if float(totales.verificado) + float(totales.pendiente) + float(monto) > precio_final:
        raise HTTPException(
            status_code=400,
            detail="El abono supera el saldo pendiente del paquete.",
        )


def _validar_saldo_membresia_gimnasio(
    db: Session,
    membresia: Optional[MembresiaGimnasio],
    monto: float,
    excluir_pago_id: Optional[int] = None,
):
    if membresia is None:
        return

    precio = float(membresia.precio or 0)

    if precio <= 0:
        return

    query = db.query(
        func.coalesce(
            func.sum(
                case((Pago.estadopago == 2, Pago.monto), else_=0)
            ),
            0,
        ).label("verificado"),
        func.coalesce(
            func.sum(
                case((Pago.estadopago == 1, Pago.monto), else_=0)
            ),
            0,
        ).label("pendiente"),
    ).filter(
        Pago.membresiagimnasioid == membresia.id,
        Pago.anulado == False,
    )

    if excluir_pago_id is not None:
        query = query.filter(Pago.id != excluir_pago_id)

    totales = query.one()

    if float(totales.verificado) + float(totales.pendiente) + float(monto) > precio:
        raise HTTPException(
            status_code=400,
            detail="El abono supera el saldo pendiente de la membresía de gimnasio.",
        )



def _nombre_paciente(paciente) -> str:
    return f"{paciente.nombres} {paciente.apellidos}".strip()


def _obtener_usuarios_verificadores_pago(
    db: Session,
    paciente,
    current_user: Usuario,
) -> list[Usuario]:
    """
    Optimizado: antes hacía 2 queries separadas, secretarios + jefes.
    Ahora hace 1 sola query con OR.
    """
    consultorio_ids = {
        cid
        for cid in (
            getattr(paciente, "consultorioid", None),
            getattr(current_user, "consultorioid", None),
        )
        if cid is not None
    }

    condicion_secretarios = Usuario.rol == 1
    if consultorio_ids:
        condicion_secretarios = and_(
            Usuario.rol == 1,
            Usuario.consultorioid.in_(list(consultorio_ids)),
        )

    return (
        db.query(Usuario)
        .filter(
            Usuario.activo == True,
            Usuario.id != current_user.id,
            or_(
                condicion_secretarios,
                Usuario.rol == 3,
            ),
        )
        .all()
    )


def _notificar_pago_transferencia_pendiente(
    db: Session,
    pago: Pago,
    paciente,
    current_user: Usuario,
) -> None:
    usuarios_destino = _obtener_usuarios_verificadores_pago(
        db=db,
        paciente=paciente,
        current_user=current_user,
    )

    if not usuarios_destino:
        print("⚠️ No se encontraron usuarios verificadores para el pago.")
        return

    nombre_paciente = _nombre_paciente(paciente)

    for usuario in usuarios_destino:
        crear_notificacion_usuario(
            db=db,
            usuarioid=usuario.id,
            titulo="Transferencia pendiente de verificación",
            mensaje=f"Hay un pago por transferencia de {nombre_paciente} pendiente de verificación.",
            tipo="pago_transferencia_pendiente",
            referencia_tipo="pago",
            referencia_id=pago.id,
            data={
                "pago_id": pago.id,
                "paciente_id": paciente.id,
                "consultorioid": paciente.consultorioid,
                "monto": float(pago.monto),
                "metodopago": pago.metodopago,
                "creado_por_id": current_user.id,
                "actualizar": [
                    "pagos",
                    "cuentas",
                    "dashboard",
                    "notificaciones",
                ],
            },
            hacer_flush=False,
        )

    db.flush()

    print(
        f"✅ Notificaciones de pago pendiente creadas: "
        f"{len(usuarios_destino)} para pago {pago.id}"
    )


def _notificar_resultado_pago_transferencia(
    db: Session,
    pago: Pago,
    paciente: Paciente,
    current_user: Usuario,
    tipo: str,
) -> None:
    if not pago.creado_por_id:
        return

    if pago.creado_por_id == current_user.id:
        return

    nombre_paciente = _nombre_paciente(paciente)

    if tipo == "pago_transferencia_verificada":
        titulo = "Pago verificado"
        mensaje = f"El pago por transferencia de {nombre_paciente} fue verificado."
    else:
        titulo = "Pago rechazado"
        mensaje = f"El pago por transferencia de {nombre_paciente} fue rechazado."

    crear_notificacion_usuario(
        db=db,
        usuarioid=pago.creado_por_id,
        titulo=titulo,
        mensaje=mensaje,
        tipo=tipo,
        referencia_tipo="pago",
        referencia_id=pago.id,
        data={
            "pago_id": pago.id,
            "paciente_id": paciente.id,
            "consultorioid": paciente.consultorioid,
            "monto": float(pago.monto),
            "metodopago": pago.metodopago,
            "estadopago": pago.estadopago,
            "verificado_por_id": current_user.id,
            "motivo_rechazo": pago.motivo_rechazo,
            "actualizar": [
                "pagos",
                "cuentas",
                "dashboard",
                "notificaciones",
            ],
        },
        hacer_flush=False,
    )

    db.flush()


# ============================================================
# LISTAR PAGOS
# ============================================================

@router.get("/", response_model=List[PagoOut])
def listar_pagos(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    # limit=20 para scroll infinito.
    # Front:
    # /api/pagos/?limit=20&offset=0
    # /api/pagos/?limit=20&offset=20
    # /api/pagos/?limit=20&offset=40

    query = (
        db.query(Pago)
        .join(Paciente, Paciente.id == Pago.pacienteid)
    )

    if current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        query = query.filter(
            _pago_visible_para_consultorio_filter(current_user.consultorioid)
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    return (
        query
        .order_by(Pago.fechapago.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )



# ============================================================
# DEUDAS PARA TERAPEUTAS
# ============================================================

def _paciente_es_ecuasanitas(paciente: Paciente) -> bool:
    tipo_seguro = (getattr(paciente, "tiposeguro", None) or "").strip().lower()
    return bool(getattr(paciente, "esecuasanitas", False)) or "ecuasanitas" in tipo_seguro


def _tratamiento_es_ecuasanitas(tratamiento: TratamientoPaciente) -> bool:
    tipo = (getattr(tratamiento, "tipotratamiento", None) or "").strip().lower()
    tipo_terapia = getattr(tratamiento, "tipo_terapia", None)
    nombre_catalogo = (getattr(tipo_terapia, "nombre", None) or "").strip().lower()
    return "ecuasanitas" in tipo or "ecuasanitas" in nombre_catalogo


def _pago_valido_para_cobertura(pago: Pago) -> bool:
    return (
        pago.estadopago == 2
        and not bool(getattr(pago, "anulado", False))
        and not bool(getattr(pago, "esrecuperacioncartera", False))
    )


def _nombre_usuario(usuario: Usuario | None) -> str:
    if not usuario:
        return "Fisioterapeuta"
    return f"{usuario.nombres or ''} {usuario.apellidos or ''}".strip() or "Fisioterapeuta"


def _nombre_paciente(paciente: Paciente) -> str:
    return f"{paciente.nombres or ''} {paciente.apellidos or ''}".strip() or "Paciente"


@router.get(
    "/mis-pacientes-deuda",
    response_model=ResumenDeudasTerapeutaOut,
)
def listar_mis_pacientes_con_deuda(
    buscar: Optional[str] = Query(default=None),
    terapeutaid: Optional[int] = Query(default=None),
    desde: Optional[date] = Query(default=None),
    hasta: Optional[date] = Query(default=None),
    modo: str = Query(default="acumulada"),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """
    Deuda real por sesiones realizadas por un fisioterapeuta.

    Corrección para pacientes compartidos Centro / Atahualpa:
    - La pantalla del fisio NO debe calcular la deuda global del paciente.
    - Solo calcula las sesiones atendidas por el fisioterapeuta seleccionado.
    - Si el paciente tiene deuda en otra sede u otro fisio, no se muestra aquí.
    - Los pagos se aplican FIFO solo contra las sesiones del fisio objetivo.
    - Cuando se puede identificar la sede del cobro, se usan pagos cobrados por
      usuarios de la misma sede del fisioterapeuta objetivo.
    """
    if current_user.rol == 2:
        terapeuta_objetivo = current_user
    elif current_user.rol in (1, 3):
        if terapeutaid is None:
            raise HTTPException(
                status_code=400,
                detail="Debes indicar el fisioterapeuta.",
            )

        terapeuta_objetivo = (
            db.query(Usuario)
            .filter(
                Usuario.id == terapeutaid,
                Usuario.rol == 2,
                Usuario.activo == True,
            )
            .first()
        )

        if not terapeuta_objetivo:
            raise HTTPException(
                status_code=404,
                detail="Fisioterapeuta no encontrado.",
            )

        if current_user.rol == 1:
            validar_consultorio_secretario(
                current_user,
                current_user.consultorioid,
            )
            if terapeuta_objetivo.consultorioid != current_user.consultorioid:
                raise HTTPException(
                    status_code=403,
                    detail="No puedes consultar fisioterapeutas de otra clínica.",
                )
    else:
        raise HTTPException(status_code=403, detail="No autorizado")

    if desde is not None and hasta is not None and desde > hasta:
        raise HTTPException(
            status_code=400,
            detail="La fecha desde no puede ser mayor que la fecha hasta.",
        )

    modo_normalizado = (modo or "acumulada").strip().lower()
    if modo_normalizado not in {"acumulada", "periodo"}:
        modo_normalizado = "acumulada"

    consultorio_operativo_id = getattr(terapeuta_objetivo, "consultorioid", None)

    # 1) Sesiones base: SOLO las sesiones realizadas por este fisio.
    # Antes se tomaban los tratamientos del fisio, pero luego se consumían pagos
    # contra todas las sesiones históricas del tratamiento. Eso mezclaba Centro
    # y Atahualpa en pacientes compartidos.
    sesiones_query = (
        db.query(SesionTerapia)
        .filter(
            SesionTerapia.terapeutaid == terapeuta_objetivo.id,
            SesionTerapia.horasalida != None,
            SesionTerapia.tratamientopacienteid != None,
        )
    )

    if hasta is not None:
        sesiones_query = sesiones_query.filter(SesionTerapia.fecha <= hasta)

    sesiones = (
        sesiones_query.order_by(
            SesionTerapia.tratamientopacienteid.asc(),
            SesionTerapia.pacienteid.asc(),
            SesionTerapia.fecha.asc(),
            SesionTerapia.horaingreso.asc(),
            SesionTerapia.id.asc(),
        )
        .all()
    )

    if not sesiones:
        return ResumenDeudasTerapeutaOut(
            terapeutaid=terapeuta_objetivo.id,
            terapeuta=_nombre_usuario(terapeuta_objetivo),
            total_deuda=0,
            pacientes_con_deuda=0,
            tratamientos_con_deuda=0,
            sesiones_pendientes=0,
            items=[],
        )

    tratamiento_ids = sorted(
        {
            int(s.tratamientopacienteid)
            for s in sesiones
            if s.tratamientopacienteid is not None
        }
    )
    paciente_ids = sorted(
        {
            int(s.pacienteid)
            for s in sesiones
            if s.pacienteid is not None
        }
    )

    if not tratamiento_ids or not paciente_ids:
        return ResumenDeudasTerapeutaOut(
            terapeutaid=terapeuta_objetivo.id,
            terapeuta=_nombre_usuario(terapeuta_objetivo),
            total_deuda=0,
            pacientes_con_deuda=0,
            tratamientos_con_deuda=0,
            sesiones_pendientes=0,
            items=[],
        )

    tratamientos_rows = (
        db.query(TratamientoPaciente, Paciente)
        .options(joinedload(TratamientoPaciente.tipo_terapia))
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
        .filter(
            TratamientoPaciente.id.in_(tratamiento_ids),
            Paciente.id.in_(paciente_ids),
        )
        .all()
    )

    tratamiento_map = {
        tratamiento.id: (tratamiento, paciente)
        for tratamiento, paciente in tratamientos_rows
    }

    # 2) Pagos válidos. Se agrupan por paciente + tratamiento.
    # Cuando el terapeuta tiene consultorio, se priorizan pagos cobrados por
    # usuarios de ese mismo consultorio. Esto evita que un pago/deuda del Centro
    # se mezcle con la vista del fisio de Atahualpa.
    pagos_query = (
        db.query(Pago)
        .filter(
            Pago.pacienteid.in_(paciente_ids),
            Pago.tratamientopacienteid.in_(tratamiento_ids),
            Pago.estadopago == 2,
            or_(Pago.anulado == False, Pago.anulado.is_(None)),
            or_(
                Pago.esrecuperacioncartera == False,
                Pago.esrecuperacioncartera.is_(None),
            ),
        )
    )

    if hasta is not None:
        pagos_query = pagos_query.filter(
            or_(
                and_(Pago.fechapagoreal != None, Pago.fechapagoreal <= hasta),
                and_(Pago.fechapagoreal == None, _fecha_caja_pago_expr() <= hasta),
            )
        )

    if consultorio_operativo_id is not None:
        CobradorDeuda = aliased(Usuario)
        pagos_query = (
            pagos_query
            .outerjoin(CobradorDeuda, CobradorDeuda.id == Pago.creado_por_id)
            .filter(
                or_(
                    CobradorDeuda.consultorioid == consultorio_operativo_id,
                    Pago.creado_por_id.is_(None),
                )
            )
        )

    pagos = (
        pagos_query.order_by(
            Pago.tratamientopacienteid.asc(),
            Pago.pacienteid.asc(),
            Pago.fechapagoreal.asc().nulls_last(),
            Pago.fechapago.asc(),
            Pago.id.asc(),
        )
        .all()
    )

    pagos_por_destino: dict[tuple[int, int], list[dict[str, float]]] = defaultdict(list)

    for pago in pagos:
        if not _pago_valido_para_cobertura(pago):
            continue
        if pago.pacienteid is None or pago.tratamientopacienteid is None:
            continue

        pagos_por_destino[(int(pago.pacienteid), int(pago.tratamientopacienteid))].append(
            {"restante": float(pago.monto or 0)}
        )

    grupos: dict[tuple[int, int], dict] = {}
    texto_busqueda = (buscar or "").strip().lower()

    # 3) FIFO real SOLO de las sesiones del fisio objetivo.
    for sesion in sesiones:
        if sesion.tratamientopacienteid not in tratamiento_map:
            continue

        tratamiento, paciente = tratamiento_map[sesion.tratamientopacienteid]

        precio = (
            float(tratamiento.precio_sesion_aplicado)
            if tratamiento.precio_sesion_aplicado is not None
            else 0.0
        )

        restante_sesion = precio
        cubierto = 0.0
        clave_pago = (int(paciente.id), int(tratamiento.id))
        pagos_fifo = pagos_por_destino.get(clave_pago, [])

        for pago_fifo in pagos_fifo:
            if restante_sesion <= 0:
                break
            if pago_fifo["restante"] <= 0:
                continue

            aplicado = min(restante_sesion, pago_fifo["restante"])
            pago_fifo["restante"] -= aplicado
            restante_sesion -= aplicado
            cubierto += aplicado

        pendiente = round(max(restante_sesion, 0.0), 2)

        if pendiente <= 0:
            continue

        if modo_normalizado == "periodo":
            if desde is not None and sesion.fecha < desde:
                continue
            if hasta is not None and sesion.fecha > hasta:
                continue

        paciente_nombre = _nombre_paciente(paciente)
        tratamiento_nombre = tratamiento.tipotratamiento or "Tratamiento"
        tipo_terapia = (
            tratamiento.tipo_terapia.nombre
            if getattr(tratamiento, "tipo_terapia", None)
            else None
        )

        if texto_busqueda:
            texto_item = " ".join(
                [
                    paciente_nombre,
                    paciente.cedula or "",
                    tratamiento_nombre,
                    tipo_terapia or "",
                ]
            ).lower()
            if texto_busqueda not in texto_item:
                continue

        key = (paciente.id, tratamiento.id)

        if key not in grupos:
            grupos[key] = {
                "pacienteid": paciente.id,
                "paciente": paciente_nombre,
                "cedula": paciente.cedula,
                "tratamientopacienteid": tratamiento.id,
                "tratamiento": tratamiento_nombre,
                "tipo_terapia": tipo_terapia,
                "terapeutaid": terapeuta_objetivo.id,
                "terapeuta": _nombre_usuario(terapeuta_objetivo),
                "precio_sesion": round(precio, 2),
                "sesiones": [],
                "total_deuda": 0.0,
            }

        grupos[key]["sesiones"].append(
            SesionPendienteTerapeutaOut(
                sesionid=sesion.id,
                fecha=sesion.fecha,
                horaingreso=(
                    sesion.horaingreso.strftime("%H:%M")
                    if sesion.horaingreso is not None
                    else None
                ),
                valor_sesion=round(precio, 2),
                cubierto=round(cubierto, 2),
                pendiente=pendiente,
            )
        )
        grupos[key]["total_deuda"] += pendiente

    items: list[PacienteDeudaTerapeutaOut] = []

    for data in grupos.values():
        sesiones_pendientes = data["sesiones"]
        total_deuda = round(float(data["total_deuda"] or 0), 2)

        if total_deuda <= 0 or not sesiones_pendientes:
            continue

        fecha_ultima = max(s.fecha for s in sesiones_pendientes)

        items.append(
            PacienteDeudaTerapeutaOut(
                pacienteid=data["pacienteid"],
                paciente=data["paciente"],
                cedula=data["cedula"],
                tratamientopacienteid=data["tratamientopacienteid"],
                tratamiento=data["tratamiento"],
                tipo_terapia=data["tipo_terapia"],
                terapeutaid=data["terapeutaid"],
                terapeuta=data["terapeuta"],
                precio_sesion=data["precio_sesion"],
                sesiones_pendientes=len(sesiones_pendientes),
                total_deuda=total_deuda,
                fecha_ultima_pendiente=fecha_ultima,
                sesiones=sesiones_pendientes,
            )
        )

    items.sort(key=lambda item: (item.paciente.lower(), item.tratamiento.lower()))

    total_deuda = round(sum(item.total_deuda for item in items), 2)
    pacientes_con_deuda = len({item.pacienteid for item in items})
    sesiones_pendientes = sum(item.sesiones_pendientes for item in items)

    return ResumenDeudasTerapeutaOut(
        terapeutaid=terapeuta_objetivo.id,
        terapeuta=_nombre_usuario(terapeuta_objetivo),
        total_deuda=total_deuda,
        pacientes_con_deuda=pacientes_con_deuda,
        tratamientos_con_deuda=len(items),
        sesiones_pendientes=sesiones_pendientes,
        items=items,
    )

# ============================================================
# CUENTAS POR PAQUETE - SISTEMA ANTERIOR
# ============================================================

@router.get("/cuentas", response_model=List[CuentaPaqueteOut])
def listar_cuentas_paquetes(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(PacientePaquete, Paciente, Paquete)
        .join(Paciente, Paciente.id == PacientePaquete.pacienteid)
        .join(Paquete, Paquete.id == PacientePaquete.paqueteid)
    )

    if current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        query = query.filter(
            Paciente.consultorioid == current_user.consultorioid
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    asignaciones = query.order_by(PacientePaquete.id.desc()).all()

    if not asignaciones:
        return []

    paquete_ids = [pp.id for pp, _, _ in asignaciones]

    pagos_rows = (
        db.query(Pago)
        .filter(Pago.pacientepaqueteid.in_(paquete_ids))
        .order_by(Pago.fechapago.asc())
        .all()
    )

    pagos_por_paquete: dict[int, list[Pago]] = defaultdict(list)

    for pago in pagos_rows:
        pagos_por_paquete[pago.pacientepaqueteid].append(pago)

    resultado = []

    for paciente_paquete, paciente, paquete in asignaciones:
        pagos = pagos_por_paquete.get(paciente_paquete.id, [])
        pagos_no_anulados = [
            pago for pago in pagos
            if not bool(getattr(pago, "anulado", False))
        ]

        precio_final = float(paciente_paquete.preciofinal)

        pagado_verificado = sum(
            float(pago.monto)
            for pago in pagos_no_anulados
            if pago.estadopago == 2
        )

        pendiente_verificacion = sum(
            float(pago.monto)
            for pago in pagos_no_anulados
            if pago.estadopago == 1
        )

        saldo = max(precio_final - pagado_verificado, 0)

        sesiones_contratadas = paciente_paquete.sesionescontratadas or 0
        sesiones_usadas = paciente_paquete.sesionesusadas or 0
        sesiones_disponibles = max(sesiones_contratadas - sesiones_usadas, 0)

        resultado.append(
            CuentaPaqueteOut(
                pacientepaqueteid=paciente_paquete.id,
                pacienteid=paciente.id,
                paciente=f"{paciente.nombres} {paciente.apellidos}",
                paquete=paquete.nombre,
                preciofinal=precio_final,
                pagado=pagado_verificado,
                saldo=saldo,
                estado_pago=_estado_cuenta(
                    total_generado=precio_final,
                    pagado=pagado_verificado,
                    saldo=saldo,
                ),
                sesionescontratadas=sesiones_contratadas,
                sesionesusadas=sesiones_usadas,
                sesionesdisponibles=sesiones_disponibles,
                duraciondias=getattr(paquete, "duracion_dias", None)
                or getattr(paquete, "duraciondias", None),
                fechaasignacion=paciente_paquete.fechaasignacion,
                fechaexpiracion=paciente_paquete.fechaexpiracion,
                pagos=[
                    PagoSimpleOut(
                        id=pago.id,
                        monto=float(pago.monto),
                        metodopago=pago.metodopago,
                        fechapago=pago.fechapago,
                        numerocomprobante=pago.numerocomprobante,
                        comprobanteurl=pago.comprobanteurl,
                        estadopago=pago.estadopago,
                        membresiagimnasioid=pago.membresiagimnasioid,
                        creado_por_id=pago.creado_por_id,
                        verificado_por_id=pago.verificado_por_id,
                        fecha_verificacion=pago.fecha_verificacion,
                        motivo_rechazo=pago.motivo_rechazo,
                        espagoprevio=bool(getattr(pago, "espagoprevio", False)),
                        fechapagoreal=getattr(pago, "fechapagoreal", None),
                        observacionpagoprevio=getattr(
                            pago,
                            "observacionpagoprevio",
                            None,
                        ),
                    )
                    for pago in pagos
                ],
            )
        )

    return resultado


# ============================================================
# CUENTAS ECUASANITAS - SOLO TERAPIAS
# ============================================================

@router.get("/cuentas-ecuasanitas", response_model=List[CuentaEcuasanitasOut])
def listar_cuentas_ecuasanitas(
    limit: int = Query(default=40, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    buscar: Optional[str] = Query(default=None),
    consultorioid: Optional[int] = Query(default=None),
    terapeuta_sesion_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """
    Lista cuánto debe cubrir/facturar Ecuasanitas por terapias recibidas.
    """
    tiene_sesiones_finalizadas = (
        exists()
        .where(SesionTerapia.tratamientopacienteid == TratamientoPaciente.id)
        .where(SesionTerapia.horasalida != None)
    )

    query = (
        db.query(TratamientoPaciente, Paciente, Usuario)
        .options(joinedload(TratamientoPaciente.tipo_terapia))
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
        .outerjoin(Usuario, Usuario.id == Paciente.terapeutaasignadoid)
        .filter(
            _condicion_paciente_ecuasanitas(),
            tiene_sesiones_finalizadas,
        )
    )

    consultorio_operativo_id: Optional[int] = None

    if current_user.rol == 2:
        query = query.filter(
            _tratamiento_visible_para_terapeuta_filter(current_user.id)
        )

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )
        consultorio_operativo_id = current_user.consultorioid
        query = query.filter(
            _tratamiento_visible_para_consultorio_filter(current_user.consultorioid)
        )

    elif current_user.rol == 3:
        if consultorioid is not None:
            consultorio_operativo_id = consultorioid
            query = query.filter(
                _tratamiento_visible_para_consultorio_filter(consultorioid)
            )

    else:
        raise HTTPException(status_code=403, detail="No autorizado")

    terapeuta_sesion_filtro = _obtener_terapeuta_para_filtro(
        db=db,
        terapeuta_sesion_id=terapeuta_sesion_id,
        current_user=current_user,
        consultorio_operativo_id=consultorio_operativo_id,
    )

    # Para rol terapeuta, la pantalla siempre debe calcular sobre las sesiones
    # que el propio terapeuta practicó, aunque no venga el query param.
    if current_user.rol == 2 and terapeuta_sesion_filtro is None:
        terapeuta_sesion_filtro = current_user

    if terapeuta_sesion_filtro is not None:
        terapeuta_operativo_id = terapeuta_sesion_filtro.id
        query = query.filter(
            _tratamiento_con_sesion_de_terapeuta_exists(
                terapeuta_sesion_filtro.id
            )
        )

    if buscar and buscar.strip():
        texto = f"%{buscar.strip()}%"
        nombre_completo = func.concat(Paciente.nombres, " ", Paciente.apellidos)

        query = query.filter(
            or_(
                Paciente.nombres.ilike(texto),
                Paciente.apellidos.ilike(texto),
                Paciente.cedula.ilike(texto),
                nombre_completo.ilike(texto),
                TratamientoPaciente.tipotratamiento.ilike(texto),
            )
        )

    tratamientos = (
        query.order_by(
            TratamientoPaciente.activo.desc(),
            TratamientoPaciente.fechainicio.desc(),
            TratamientoPaciente.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not tratamientos:
        return []

    tratamiento_ids = [
        tratamiento.id
        for tratamiento, _, _ in tratamientos
    ]

    sesiones_rows = (
        db.query(
            SesionTerapia.tratamientopacienteid,
            func.count(SesionTerapia.id).label("sesiones_cubiertas"),
            func.max(SesionTerapia.fecha).label("fecha_ultima_sesion"),
        )
        .filter(
            SesionTerapia.tratamientopacienteid.in_(tratamiento_ids),
            SesionTerapia.horasalida != None,
        )
        .group_by(SesionTerapia.tratamientopacienteid)
        .all()
    )

    sesiones_por_tratamiento = {
        tratamiento_id: {
            "sesiones": int(sesiones or 0),
            "ultima": ultima,
        }
        for tratamiento_id, sesiones, ultima in sesiones_rows
    }

    resultado: list[CuentaEcuasanitasOut] = []

    for tratamiento, paciente, terapeuta in tratamientos:
        data_sesiones = sesiones_por_tratamiento.get(tratamiento.id, {})
        sesiones_cubiertas = int(data_sesiones.get("sesiones", 0) or 0)

        if sesiones_cubiertas <= 0:
            continue

        precio_sesion = (
            float(tratamiento.precio_sesion_aplicado)
            if tratamiento.precio_sesion_aplicado is not None
            else 0.0
        )

        total_cubierto = round(precio_sesion * sesiones_cubiertas, 2)

        nombre_terapeuta = None
        if terapeuta:
            nombre_terapeuta = f"{terapeuta.nombres} {terapeuta.apellidos}".strip()

        nombre_tipo_terapia = None
        if tratamiento.tipo_terapia:
            nombre_tipo_terapia = tratamiento.tipo_terapia.nombre

        resultado.append(
            CuentaEcuasanitasOut(
                tratamientopacienteid=tratamiento.id,
                pacienteid=paciente.id,
                paciente=f"{paciente.nombres} {paciente.apellidos}".strip(),
                terapeutaid=paciente.terapeutaasignadoid,
                terapeuta=nombre_terapeuta,
                tratamiento=tratamiento.tipotratamiento,
                tipoterapiaid=tratamiento.tipoterapiaid,
                tipo_terapia=nombre_tipo_terapia,
                precio_sesion_aplicado=precio_sesion,
                sesiones_cubiertas=sesiones_cubiertas,
                total_cubierto=total_cubierto,
                ganancia_terapeuta=round(
                    total_cubierto * PORCENTAJE_FISIO_TERAPIA,
                    2,
                ),
                valor_clinica=round(
                    total_cubierto * PORCENTAJE_CLINICA_TERAPIA,
                    2,
                ),
                fecha_ultima_sesion=data_sesiones.get("ultima"),
                estado="POR FACTURAR",
            )
        )

    return resultado


# ============================================================
# CUENTAS POR TRATAMIENTO - NUEVO SISTEMA
# ============================================================

@router.get("/cuentas-tratamientos", response_model=List[CuentaTratamientoOut])
def listar_cuentas_tratamientos(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    buscar: Optional[str] = Query(default=None),
    solo_transferencias_pendientes: bool = Query(default=False),
    consultorioid: Optional[int] = Query(default=None),
    terapeuta_sesion_id: Optional[int] = Query(default=None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(TratamientoPaciente, Paciente)
        .options(joinedload(TratamientoPaciente.tipo_terapia))
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
    )

    consultorio_operativo_id: Optional[int] = None
    terapeuta_operativo_id: Optional[int] = None

    if current_user.rol == 2:
        terapeuta_operativo_id = current_user.id

        query = query.filter(
            _tratamiento_visible_para_terapeuta_filter(current_user.id)
        )

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )

        consultorio_operativo_id = current_user.consultorioid

        query = query.filter(
            _tratamiento_visible_para_consultorio_filter(
                current_user.consultorioid
            )
        )

    elif current_user.rol == 3:
        # Jefe:
        # - Sin consultorioid: ve cálculo global.
        # - Con consultorioid: ve cálculo operativo de esa sucursal.
        if consultorioid is not None:
            consultorio_operativo_id = consultorioid
            query = query.filter(
                _tratamiento_visible_para_consultorio_filter(consultorioid)
            )

    else:
        raise HTTPException(status_code=403, detail="No autorizado")

    terapeuta_sesion_filtro = _obtener_terapeuta_para_filtro(
        db=db,
        terapeuta_sesion_id=terapeuta_sesion_id,
        current_user=current_user,
        consultorio_operativo_id=consultorio_operativo_id,
    )

    # Para rol terapeuta, la pantalla siempre debe calcular sobre sus propias sesiones
    # aunque no venga el query param desde el front.
    if current_user.rol == 2 and terapeuta_sesion_filtro is None:
        terapeuta_sesion_filtro = current_user

    if terapeuta_sesion_filtro is not None:
        terapeuta_operativo_id = terapeuta_sesion_filtro.id
        query = query.filter(
            _tratamiento_con_sesion_de_terapeuta_exists(
                terapeuta_sesion_filtro.id
            )
        )

    if buscar and buscar.strip():
        texto = f"%{buscar.strip()}%"
        nombre_completo = func.concat(
            Paciente.nombres,
            " ",
            Paciente.apellidos,
        )

        query = query.filter(
            or_(
                Paciente.nombres.ilike(texto),
                Paciente.apellidos.ilike(texto),
                nombre_completo.ilike(texto),
                TratamientoPaciente.tipotratamiento.ilike(texto),
            )
        )

    if solo_transferencias_pendientes:
        condiciones_transferencia = [
            Pago.tratamientopacienteid == TratamientoPaciente.id,
            Pago.estadopago == 1,
            or_(Pago.anulado == False, Pago.anulado.is_(None)),
            Pago.metodopago.ilike("%transfer%"),
        ]

        # Si estoy viendo una sucursal específica, solo quiero las
        # transferencias registradas/cobradas por usuarios de esa sucursal.
        if consultorio_operativo_id is not None:
            condiciones_transferencia.extend(
                [
                    Pago.creado_por_id == Usuario.id,
                    Usuario.consultorioid == consultorio_operativo_id,
                ]
            )

        tiene_transferencia_pendiente = exists().where(
            and_(*condiciones_transferencia)
        )

        query = query.filter(tiene_transferencia_pendiente)

    tratamientos = (
        query.order_by(
            TratamientoPaciente.activo.desc(),
            TratamientoPaciente.fechainicio.desc(),
            TratamientoPaciente.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not tratamientos:
        return []

    tratamiento_ids = [
        tratamiento.id
        for tratamiento, _ in tratamientos
    ]

    cobertura_por_fisio = {}
    if terapeuta_sesion_filtro is not None:
        cobertura_por_fisio = _calcular_cobertura_fifo_por_terapeuta(
            db=db,
            tratamiento_ids=tratamiento_ids,
            terapeutaid=terapeuta_sesion_filtro.id,
        )

    # ============================================================
    # SESIONES
    # Importante:
    # - Secretaria Atahualpa: cuenta solo sesiones hechas por terapeutas
    #   de Atahualpa.
    # - Secretaria Centro: cuenta solo sesiones hechas por terapeutas
    #   del Centro.
    # - Terapeuta: cuenta solo sus propias sesiones.
    # - Jefe sin filtro: cuenta todo.
    # ============================================================

    sesiones_query = (
        db.query(
            SesionTerapia.tratamientopacienteid,
            func.count(SesionTerapia.id),
        )
        .select_from(SesionTerapia)
        .filter(
            SesionTerapia.tratamientopacienteid.in_(tratamiento_ids),
            SesionTerapia.horasalida != None,
        )
    )

    if terapeuta_operativo_id is not None:
        sesiones_query = sesiones_query.filter(
            SesionTerapia.terapeutaid == terapeuta_operativo_id
        )

    elif consultorio_operativo_id is not None:
        TerapeutaSesion = aliased(Usuario)

        sesiones_query = (
            sesiones_query
            .join(
                TerapeutaSesion,
                TerapeutaSesion.id == SesionTerapia.terapeutaid,
            )
            .filter(
                TerapeutaSesion.consultorioid == consultorio_operativo_id
            )
        )

    sesiones_rows = (
        sesiones_query
        .group_by(SesionTerapia.tratamientopacienteid)
        .all()
    )

    sesiones_por_tratamiento = {
        tratamiento_id: int(total or 0)
        for tratamiento_id, total in sesiones_rows
    }

    # ============================================================
    # PAGOS
    # Se filtran por la sucursal que registró/cobró el pago.
    #
    # Nota:
    # Esta es la mejor solución con tu estructura actual.
    # Lo ideal más adelante sería agregar Pago.consultorioid_cobro
    # para no depender de creado_por_id.
    # ============================================================

    pago_no_anulado = or_(Pago.anulado == False, Pago.anulado.is_(None))
    no_es_recuperacion_cartera = or_(
        Pago.esrecuperacioncartera == False,
        Pago.esrecuperacioncartera.is_(None),
    )
    no_es_pago_previo = or_(
        Pago.espagoprevio == False,
        Pago.espagoprevio.is_(None),
    )

    pagos_agregados_query = (
        db.query(
            Pago.tratamientopacienteid.label("tratamiento_id"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Pago.estadopago == 2,
                                pago_no_anulado,
                                no_es_recuperacion_cartera,
                                no_es_pago_previo,
                            ),
                            Pago.monto,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pagado_caja_verificado"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Pago.estadopago == 2,
                                pago_no_anulado,
                                no_es_recuperacion_cartera,
                                Pago.espagoprevio == True,
                            ),
                            Pago.monto,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pago_previo_verificado"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                Pago.estadopago == 1,
                                pago_no_anulado,
                                no_es_recuperacion_cartera,
                            ),
                            Pago.monto,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("pendiente_verificacion"),
        )
        .filter(Pago.tratamientopacienteid.in_(tratamiento_ids))
    )

    pagos_detalle_query = (
        db.query(Pago)
        .filter(Pago.tratamientopacienteid.in_(tratamiento_ids))
    )

    if consultorio_operativo_id is not None:
        CobradorAgregado = aliased(Usuario)
        CobradorDetalle = aliased(Usuario)

        pagos_agregados_query = (
            pagos_agregados_query
            .join(
                CobradorAgregado,
                CobradorAgregado.id == Pago.creado_por_id,
            )
            .filter(
                CobradorAgregado.consultorioid == consultorio_operativo_id
            )
        )

        pagos_detalle_query = (
            pagos_detalle_query
            .join(
                CobradorDetalle,
                CobradorDetalle.id == Pago.creado_por_id,
            )
            .filter(
                CobradorDetalle.consultorioid == consultorio_operativo_id
            )
        )

    pagos_agregados_rows = (
        pagos_agregados_query
        .group_by(Pago.tratamientopacienteid)
        .all()
    )

    pagos_totales_por_tratamiento = {
        row.tratamiento_id: {
            "pagado_caja_verificado": float(row.pagado_caja_verificado or 0),
            "pago_previo_verificado": float(row.pago_previo_verificado or 0),
            "pendiente_verificacion": float(row.pendiente_verificacion or 0),
        }
        for row in pagos_agregados_rows
    }

    pagos_rows = (
        pagos_detalle_query
        .order_by(Pago.fechapago.asc())
        .all()
    )

    pagos_por_tratamiento = defaultdict(list)

    for pago in pagos_rows:
        pagos_por_tratamiento[pago.tratamientopacienteid].append(pago)

    resultado = []

    for tratamiento, paciente in tratamientos:
        sesiones_realizadas = sesiones_por_tratamiento.get(tratamiento.id, 0)

        precio_aplicado = (
            float(tratamiento.precio_sesion_aplicado)
            if tratamiento.precio_sesion_aplicado is not None
            else 0.0
        )

        total_generado = float(sesiones_realizadas) * precio_aplicado

        pagos_totales = pagos_totales_por_tratamiento.get(
            tratamiento.id,
            {
                "pagado_caja_verificado": 0.0,
                "pago_previo_verificado": 0.0,
                "pendiente_verificacion": 0.0,
            },
        )

        pagado_caja_verificado = float(
            pagos_totales["pagado_caja_verificado"]
        )
        pago_previo_verificado = float(
            pagos_totales["pago_previo_verificado"]
        )
        pendiente_verificacion = float(
            pagos_totales["pendiente_verificacion"]
        )

        pagado_verificado = pagado_caja_verificado + pago_previo_verificado

        saldo = max(total_generado - pagado_verificado, 0)
        saldo_favor = max(pagado_verificado - total_generado, 0)

        if terapeuta_sesion_filtro is not None:
            cobertura = cobertura_por_fisio.get(tratamiento.id)
            if not cobertura:
                continue

            sesiones_realizadas = int(cobertura["sesiones_realizadas"] or 0)
            total_generado = float(cobertura["total_generado"] or 0)
            pagado_caja_verificado = float(
                cobertura["pagado_caja_verificado"] or 0
            )
            pago_previo_verificado = float(
                cobertura["pago_previo_verificado"] or 0
            )
            pagado_verificado = float(cobertura["pagado_verificado"] or 0)
            saldo = float(cobertura["saldo"] or 0)
            saldo_favor = 0.0

        nombre_tipo_terapia = None
        if tratamiento.tipo_terapia:
            nombre_tipo_terapia = tratamiento.tipo_terapia.nombre

        pagos = pagos_por_tratamiento.get(tratamiento.id, [])

        resultado.append(
            CuentaTratamientoOut(
                tratamientopacienteid=tratamiento.id,
                pacienteid=paciente.id,
                paciente=f"{paciente.nombres} {paciente.apellidos}",
                terapeuta_sesionid=(
                    terapeuta_sesion_filtro.id
                    if terapeuta_sesion_filtro is not None
                    else None
                ),
                terapeuta_sesion=(
                    f"{terapeuta_sesion_filtro.nombres} {terapeuta_sesion_filtro.apellidos}"
                    if terapeuta_sesion_filtro is not None
                    else None
                ),
                tratamiento=tratamiento.tipotratamiento,
                tipoterapiaid=tratamiento.tipoterapiaid,
                tipo_terapia=nombre_tipo_terapia,
                precio_sesion_oficial=(
                    float(tratamiento.precio_sesion_oficial)
                    if tratamiento.precio_sesion_oficial is not None
                    else None
                ),
                precio_sesion_aplicado=(
                    float(tratamiento.precio_sesion_aplicado)
                    if tratamiento.precio_sesion_aplicado is not None
                    else None
                ),
                sesiones_estimadas=tratamiento.sesiones_estimadas,
                sesiones_realizadas=sesiones_realizadas,
                total_generado=total_generado,
                pagado_verificado=pagado_verificado,
                pago_previo_verificado=pago_previo_verificado,
                pagado_caja_verificado=pagado_caja_verificado,
                pendiente_verificacion=pendiente_verificacion,
                saldo=saldo,
                saldo_favor=saldo_favor,
                estado_pago=_estado_cuenta(
                    total_generado=total_generado,
                    pagado=pagado_verificado,
                    saldo=saldo,
                ),
                motivo_precio_especial=tratamiento.motivo_precio_especial,
                fechainicio=tratamiento.fechainicio,
                activo=tratamiento.activo,
                pagos=[
                    PagoSimpleOut(
                        id=pago.id,
                        monto=float(pago.monto or 0),
                        metodopago=pago.metodopago,
                        fechapago=pago.fechapago,
                        numerocomprobante=pago.numerocomprobante,
                        comprobanteurl=pago.comprobanteurl,
                        estadopago=pago.estadopago,
                        membresiagimnasioid=pago.membresiagimnasioid,
                        creado_por_id=pago.creado_por_id,
                        verificado_por_id=pago.verificado_por_id,
                        fecha_verificacion=pago.fecha_verificacion,
                        motivo_rechazo=pago.motivo_rechazo,
                        espagoprevio=bool(
                            getattr(pago, "espagoprevio", False)
                        ),
                        fechapagoreal=getattr(pago, "fechapagoreal", None),
                        observacionpagoprevio=getattr(
                            pago,
                            "observacionpagoprevio",
                            None,
                        ),
                        esrecuperacioncartera=bool(
                            getattr(pago, "esrecuperacioncartera", False)
                        ),
                        observacion_cartera=getattr(
                            pago,
                            "observacion_cartera",
                            None,
                        ),
                        anulado=bool(pago.anulado),
                        anulado_por_id=pago.anulado_por_id,
                        fecha_anulacion=pago.fecha_anulacion,
                        motivo_anulacion=pago.motivo_anulacion,
                    )
                    for pago in pagos
                ],
            )
        )

    return resultado


# ============================================================
# CUENTAS POR GIMNASIO
# ============================================================

@router.get("/cuentas-gimnasio", response_model=List[CuentaMembresiaGimnasioOut])
def listar_cuentas_gimnasio(
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    buscar: Optional[str] = Query(default=None),
    solo_transferencias_pendientes: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(MembresiaGimnasio, Paciente)
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
        .filter(MembresiaGimnasio.modalidad == "MENSUAL")
    )

    if current_user.rol == 2:
        query = query.filter(Paciente.terapeutaasignadoid == current_user.id)

    elif current_user.rol == 1:
        validar_consultorio_secretario(
            current_user,
            current_user.consultorioid,
        )
        query = query.filter(Paciente.consultorioid == current_user.consultorioid)

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(status_code=403, detail="No autorizado")

    if buscar and buscar.strip():
        texto = f"%{buscar.strip()}%"

        query = query.filter(
            or_(
                Paciente.nombres.ilike(texto),
                Paciente.apellidos.ilike(texto),
                MembresiaGimnasio.observaciones.ilike(texto),
            )
        )
    
    if solo_transferencias_pendientes:
        tiene_transferencia_pendiente = (
            exists()
            .where(Pago.membresiagimnasioid == MembresiaGimnasio.id)
            .where(Pago.estadopago == 1)
            .where(Pago.anulado == False)
            .where(Pago.metodopago.ilike("%transfer%"))
        )

        query = query.filter(tiene_transferencia_pendiente)

    membresias = (
        query.order_by(
            MembresiaGimnasio.activo.desc(),
            MembresiaGimnasio.fechainicio.desc(),
            MembresiaGimnasio.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    if not membresias:
        return []

    membresia_ids = [
        membresia.id
        for membresia, _ in membresias
    ]

    pagos_rows = (
        db.query(Pago)
        .filter(Pago.membresiagimnasioid.in_(membresia_ids))
        .order_by(Pago.fechapago.asc())
        .all()
    )

    pagos_por_membresia = defaultdict(list)

    for pago in pagos_rows:
        pagos_por_membresia[pago.membresiagimnasioid].append(pago)

    resultado = []

    for membresia, paciente in membresias:
        precio = float(membresia.precio or 0)

        pagos = pagos_por_membresia.get(membresia.id, [])
        pagos_no_anulados = [
            pago for pago in pagos
            if not bool(pago.anulado)
        ]

        pagado_caja_verificado = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 2
            and not bool(getattr(pago, "espagoprevio", False))
        )

        pago_previo_verificado = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 2
            and bool(getattr(pago, "espagoprevio", False))
        )

        pagado_verificado = pagado_caja_verificado + pago_previo_verificado

        pendiente_verificacion = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 1
        )

        saldo = max(precio - pagado_verificado, 0)
        saldo_favor = max(pagado_verificado - precio, 0)

        resultado.append(
            CuentaMembresiaGimnasioOut(
                membresiagimnasioid=membresia.id,
                pacienteid=paciente.id,
                paciente=f"{paciente.nombres} {paciente.apellidos}",
                fechainicio=membresia.fechainicio,
                diascontratados=int(membresia.diascontratados or 20),
                precio=precio,
                activo=membresia.activo,
                observaciones=membresia.observaciones,
                pagado_verificado=pagado_verificado,
                pago_previo_verificado=pago_previo_verificado,
                pagado_caja_verificado=pagado_caja_verificado,
                pendiente_verificacion=pendiente_verificacion,
                saldo=saldo,
                saldo_favor=saldo_favor,
                estado_pago=_estado_cuenta(
                    total_generado=precio,
                    pagado=pagado_verificado,
                    saldo=saldo,
                ),
                pagos=[
                    PagoSimpleOut(
                        id=pago.id,
                        monto=float(pago.monto or 0),
                        metodopago=pago.metodopago,
                        fechapago=pago.fechapago,
                        numerocomprobante=pago.numerocomprobante,
                        comprobanteurl=pago.comprobanteurl,
                        estadopago=pago.estadopago,
                        membresiagimnasioid=pago.membresiagimnasioid,
                        creado_por_id=pago.creado_por_id,
                        verificado_por_id=pago.verificado_por_id,
                        fecha_verificacion=pago.fecha_verificacion,
                        motivo_rechazo=pago.motivo_rechazo,
                        espagoprevio=bool(getattr(pago, "espagoprevio", False)),
                        fechapagoreal=getattr(pago, "fechapagoreal", None),
                        observacionpagoprevio=getattr(
                            pago,
                            "observacionpagoprevio",
                            None,
                        ),
                        anulado=bool(pago.anulado),
                        anulado_por_id=pago.anulado_por_id,
                        fecha_anulacion=pago.fecha_anulacion,
                        motivo_anulacion=pago.motivo_anulacion,
                    )
                    for pago in pagos
                ],
            )
        )

    return resultado


# ============================================================
# REGISTRAR PAGO PREVIO DE TERAPIAS
# ============================================================

@router.post(
    "/pago-previo-tratamiento",
    response_model=PagoOut,
    status_code=status.HTTP_201_CREATED,
)
def registrar_pago_previo_tratamiento(
    data: PagoPrevioTratamientoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    """
    Registra dinero que el paciente ya había pagado antes de usar el sistema.

    Este movimiento reduce el saldo del tratamiento, pero no debe contarse
    como ingreso de caja del día.
    """
    paciente = _validar_paciente(
        db=db,
        pacienteid=data.pacienteid,
        current_user=current_user,
    )

    _validar_tratamiento_paciente(
        db=db,
        pacienteid=data.pacienteid,
        tratamientopacienteid=data.tratamientopacienteid,
    )

    observacion = (data.observacionpagoprevio or "").strip() or None

    nuevo_pago = Pago(
        pacienteid=data.pacienteid,
        pacientepaqueteid=None,
        tratamientopacienteid=data.tratamientopacienteid,
        membresiagimnasioid=None,
        monto=float(data.monto),
        metodopago="Pago previo",
        numerocomprobante=None,
        comprobanteurl=None,
        estadopago=2,
        creado_por_id=current_user.id,
        verificado_por_id=current_user.id,
        fechapago=now_utc(),
        fecha_verificacion=now_utc(),
        motivo_rechazo=None,
        espagoprevio=True,
        fechapagoreal=data.fechapagoreal,
        observacionpagoprevio=observacion,
    )

    db.add(nuevo_pago)
    db.commit()
    db.refresh(nuevo_pago)

    print(
        f"✅ Pago previo registrado para paciente {paciente.id} "
        f"tratamiento {data.tratamientopacienteid}: ${float(data.monto):.2f}"
    )

    return nuevo_pago


# ============================================================
# REGISTRAR PAGO PREVIO DE GIMNASIO
# ============================================================

@router.post(
    "/pago-previo-gimnasio",
    response_model=PagoOut,
    status_code=status.HTTP_201_CREATED,
)
def registrar_pago_previo_gimnasio(
    data: PagoPrevioGimnasioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    """
    Registra dinero que el paciente ya había pagado por gimnasio antes
    de usar el sistema.
    """
    paciente = _validar_paciente(
        db=db,
        pacienteid=data.pacienteid,
        current_user=current_user,
    )

    membresia = _validar_membresia_gimnasio(
        db=db,
        pacienteid=data.pacienteid,
        membresiagimnasioid=data.membresiagimnasioid,
    )

    _validar_saldo_membresia_gimnasio(
        db=db,
        membresia=membresia,
        monto=float(data.monto),
    )

    observacion = (data.observacionpagoprevio or "").strip() or None

    nuevo_pago = Pago(
        pacienteid=data.pacienteid,
        pacientepaqueteid=None,
        tratamientopacienteid=None,
        membresiagimnasioid=data.membresiagimnasioid,
        monto=float(data.monto),
        metodopago="Pago previo",
        numerocomprobante=None,
        comprobanteurl=None,
        estadopago=2,
        creado_por_id=current_user.id,
        verificado_por_id=current_user.id,
        fechapago=now_utc(),
        fecha_verificacion=now_utc(),
        motivo_rechazo=None,
        espagoprevio=True,
        fechapagoreal=data.fechapagoreal,
        observacionpagoprevio=observacion,
    )

    db.add(nuevo_pago)
    db.commit()
    db.refresh(nuevo_pago)

    print(
        f"✅ Pago previo de gimnasio registrado para paciente {paciente.id} "
        f"membresía {data.membresiagimnasioid}: ${float(data.monto):.2f}"
    )

    return nuevo_pago


# ============================================================
# RECUPERACIÓN DE CARTERA
# Dinero cobrado hoy por atenciones anteriores al sistema.
# SÍ entra a caja. NO reduce saldos ni genera comisión automática.
# ============================================================

@router.post(
    "/recuperacion-cartera",
    response_model=PagoOut,
    status_code=status.HTTP_201_CREATED,
)
def registrar_recuperacion_cartera(
    data: RecuperacionCarteraCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    """
    Registra dinero cobrado hoy por deuda anterior al inicio del sistema.

    Ejemplo: el paciente paga $36 hoy; $9 pertenecen a la sesión registrada
    esta semana y $27 corresponden a atenciones anteriores que no existen en
    el sistema. Los $27 se registran aquí para que entren a caja sin crear
    saldo a favor ni sesiones falsas.
    """
    _validar_paciente(
        db=db,
        pacienteid=data.pacienteid,
        current_user=current_user,
    )

    metodo = (data.metodopago or "").strip()

    if not metodo:
        raise HTTPException(
            status_code=400,
            detail="Seleccione un método de pago.",
        )

    if _es_transferencia(metodo):
        raise HTTPException(
            status_code=400,
            detail=(
                "Por ahora la recuperación de cartera solo acepta efectivo "
                "o tarjeta. Para transferencias se debe registrar y verificar "
                "con comprobante en una actualización posterior."
            ),
        )

    observacion = (data.observacion_cartera or "").strip() or None

    nuevo_pago = Pago(
        pacienteid=data.pacienteid,
        pacientepaqueteid=None,
        tratamientopacienteid=None,
        membresiagimnasioid=None,
        monto=float(data.monto),
        metodopago=metodo,
        numerocomprobante=None,
        comprobanteurl=None,
        estadopago=2,
        creado_por_id=current_user.id,
        verificado_por_id=current_user.id,
        fechapago=now_utc(),
        fecha_verificacion=now_utc(),
        motivo_rechazo=None,
        espagoprevio=False,
        esrecuperacioncartera=True,
        fechapagoreal=data.fechapagoreal,
        observacion_cartera=observacion,
    )

    db.add(nuevo_pago)
    db.flush()

    respuesta = PagoOut.model_validate(nuevo_pago)

    db.commit()

    return respuesta


# ============================================================
# URL FIRMADA DEL COMPROBANTE
# ============================================================

@router.get("/{pago_id}/comprobante-url")
def obtener_url_comprobante(
    pago_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    pago, _ = _obtener_pago_con_acceso(
        db=db,
        pago_id=pago_id,
        current_user=current_user,
    )

    if not pago.comprobanteurl:
        raise HTTPException(
            status_code=404,
            detail="Este pago no tiene comprobante registrado.",
        )

    url = crear_url_firmada_comprobante(
        pago.comprobanteurl,
        segundos=3600,
    )

    return {
        "url": url,
        "expira_en_segundos": 3600,
    }


# ============================================================
# REGISTRAR PAGO SIN COMPROBANTE
# Efectivo / Tarjeta => verificado automáticamente
# ============================================================

@router.post("/", response_model=PagoOut, status_code=status.HTTP_201_CREATED)
def registrar_pago(
    pago: PagoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    _validar_destino_pago(
        pacientepaqueteid=pago.pacientepaqueteid,
        tratamientopacienteid=pago.tratamientopacienteid,
        membresiagimnasioid=pago.membresiagimnasioid,
    )

    _validar_paciente(
        db=db,
        pacienteid=pago.pacienteid,
        current_user=current_user,
        tratamientopacienteid=pago.tratamientopacienteid,
        permitir_atencion_compartida=True,
    )

    paciente_paquete = _validar_paciente_paquete(
        db=db,
        pacienteid=pago.pacienteid,
        pacientepaqueteid=pago.pacientepaqueteid,
    )

    _validar_tratamiento_paciente(
        db=db,
        pacienteid=pago.pacienteid,
        tratamientopacienteid=pago.tratamientopacienteid,
    )

    membresia_gimnasio = _validar_membresia_gimnasio(
        db=db,
        pacienteid=pago.pacienteid,
        membresiagimnasioid=pago.membresiagimnasioid,
    )

    metodo = (pago.metodopago or "").strip()

    if not metodo:
        raise HTTPException(
            status_code=400,
            detail="Seleccione un método de pago.",
        )

    if _es_transferencia(metodo):
        raise HTTPException(
            status_code=400,
            detail="Para pagos por transferencia debe usar el registro con comprobante.",
        )

    _validar_saldo_paquete(
        db=db,
        paciente_paquete=paciente_paquete,
        monto=pago.monto,
    )

    _validar_saldo_membresia_gimnasio(
        db=db,
        membresia=membresia_gimnasio,
        monto=pago.monto,
    )

    data = pago.model_dump()

    data["metodopago"] = metodo
    data["numerocomprobante"] = None
    data["comprobanteurl"] = None
    data["estadopago"] = 2
    data["creado_por_id"] = current_user.id
    data["verificado_por_id"] = current_user.id
    data["fechapago"] = now_utc()
    data["fecha_verificacion"] = now_utc()
    data["motivo_rechazo"] = None

    nuevo_pago = Pago(**data)

    db.add(nuevo_pago)
    db.flush()

    respuesta = PagoOut.model_validate(nuevo_pago)

    db.commit()

    return respuesta



# ============================================================
# REGISTRAR TRANSFERENCIA GRUPAL / COMPARTIDA
# Un solo comprobante físico, varios pagos internos.
# ============================================================

@router.post(
    "/registrar-transferencia-grupal",
    response_model=List[PagoOut],
    status_code=status.HTTP_201_CREATED,
)
async def registrar_transferencia_grupal(
    total_transferencia: float = Form(...),
    numerocomprobante: str = Form(...),
    items_json: str = Form(...),
    comprobante: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    """
    Registra una transferencia compartida.

    Caso real:
    - Una sola transferencia bancaria por $30.
    - El comprobante pertenece a 3 pacientes.
    - En la base se crean 3 pagos separados para que reportes, saldos,
      fisios y pacientes cuadren correctamente.

    No requiere columnas nuevas en la tabla pagos: todos los pagos quedan
    unidos por el mismo número de comprobante y la misma URL de comprobante.
    """
    numero = (numerocomprobante or "").strip()

    if not numero or len(numero) < 3:
        raise HTTPException(
            status_code=400,
            detail="Ingrese un número de comprobante válido.",
        )

    if total_transferencia <= 0:
        raise HTTPException(
            status_code=400,
            detail="El total de la transferencia debe ser mayor a 0.",
        )

    if comprobante is None:
        raise HTTPException(
            status_code=400,
            detail="Debe subir la foto del comprobante.",
        )

    try:
        raw_items = json.loads(items_json or "[]")
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400,
            detail="La distribución del pago grupal no tiene un formato válido.",
        )

    if not isinstance(raw_items, list) or len(raw_items) < 2:
        raise HTTPException(
            status_code=400,
            detail="Agregue al menos 2 pacientes para registrar un pago grupal.",
        )

    items: list[dict] = []
    total_distribuido = 0.0

    for index, item in enumerate(raw_items, start=1):
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=400,
                detail=f"El detalle #{index} del pago grupal no es válido.",
            )

        try:
            pacienteid = int(item.get("pacienteid"))
            monto = float(item.get("monto"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail=f"Revise paciente y monto del detalle #{index}.",
            )

        tratamientopacienteid = item.get("tratamientopacienteid")
        pacientepaqueteid = item.get("pacientepaqueteid")
        membresiagimnasioid = item.get("membresiagimnasioid")

        tratamientopacienteid = int(tratamientopacienteid) if tratamientopacienteid else None
        pacientepaqueteid = int(pacientepaqueteid) if pacientepaqueteid else None
        membresiagimnasioid = int(membresiagimnasioid) if membresiagimnasioid else None

        if monto <= 0:
            raise HTTPException(
                status_code=400,
                detail=f"El monto del detalle #{index} debe ser mayor a 0.",
            )

        _validar_destino_pago(
            pacientepaqueteid=pacientepaqueteid,
            tratamientopacienteid=tratamientopacienteid,
            membresiagimnasioid=membresiagimnasioid,
        )

        paciente = _validar_paciente(
            db=db,
            pacienteid=pacienteid,
            current_user=current_user,
            tratamientopacienteid=tratamientopacienteid,
            permitir_atencion_compartida=True,
        )

        paciente_paquete = _validar_paciente_paquete(
            db=db,
            pacienteid=pacienteid,
            pacientepaqueteid=pacientepaqueteid,
        )

        _validar_tratamiento_paciente(
            db=db,
            pacienteid=pacienteid,
            tratamientopacienteid=tratamientopacienteid,
        )

        membresia_gimnasio = _validar_membresia_gimnasio(
            db=db,
            pacienteid=pacienteid,
            membresiagimnasioid=membresiagimnasioid,
        )

        _validar_saldo_paquete(
            db=db,
            paciente_paquete=paciente_paquete,
            monto=monto,
        )

        _validar_saldo_membresia_gimnasio(
            db=db,
            membresia=membresia_gimnasio,
            monto=monto,
        )

        items.append(
            {
                "pacienteid": pacienteid,
                "pacientepaqueteid": pacientepaqueteid,
                "tratamientopacienteid": tratamientopacienteid,
                "membresiagimnasioid": membresiagimnasioid,
                "monto": round(monto, 2),
                "paciente_ref": SimpleNamespace(
                    id=paciente.id,
                    consultorioid=paciente.consultorioid,
                    nombres=paciente.nombres,
                    apellidos=paciente.apellidos,
                ),
            }
        )
        total_distribuido += monto

    if abs(round(total_distribuido, 2) - round(float(total_transferencia), 2)) > 0.01:
        raise HTTPException(
            status_code=400,
            detail=(
                "El total distribuido no coincide con el total de la transferencia. "
                f"Transferencia: ${float(total_transferencia):.2f}. "
                f"Distribuido: ${total_distribuido:.2f}."
            ),
        )

    current_user_id = current_user.id
    primer_paciente_id = items[0]["pacienteid"]

    # Liberar la transacción antes de subir la imagen.
    db.rollback()
    db.close()

    comprobante_path = await subir_comprobante_pago(
        comprobante,
        primer_paciente_id,
    )

    try:
        current_user_db = (
            db.query(Usuario)
            .filter(
                Usuario.id == current_user_id,
                Usuario.activo == True,
            )
            .first()
        )

        if not current_user_db:
            raise HTTPException(
                status_code=401,
                detail="Usuario no encontrado o inactivo.",
            )

        pagos_creados: list[Pago] = []

        for item in items:
            # Revalidar acceso después del upload.
            _validar_paciente(
                db=db,
                pacienteid=item["pacienteid"],
                current_user=current_user_db,
                tratamientopacienteid=item["tratamientopacienteid"],
                permitir_atencion_compartida=True,
            )

            _validar_tratamiento_paciente(
                db=db,
                pacienteid=item["pacienteid"],
                tratamientopacienteid=item["tratamientopacienteid"],
            )

            pago = Pago(
                pacienteid=item["pacienteid"],
                pacientepaqueteid=item["pacientepaqueteid"],
                tratamientopacienteid=item["tratamientopacienteid"],
                membresiagimnasioid=item["membresiagimnasioid"],
                monto=item["monto"],
                metodopago="Transferencia",
                numerocomprobante=numero,
                comprobanteurl=comprobante_path,
                estadopago=1,
                creado_por_id=current_user_db.id,
                verificado_por_id=None,
                fechapago=now_utc(),
                fecha_verificacion=None,
                motivo_rechazo=None,
            )

            db.add(pago)
            db.flush()
            pagos_creados.append(pago)

            _notificar_pago_transferencia_pendiente(
                db=db,
                pago=pago,
                paciente=item["paciente_ref"],
                current_user=current_user_db,
            )

        respuesta = [PagoOut.model_validate(pago) for pago in pagos_creados]

        db.commit()
        return respuesta

    except Exception:
        db.rollback()
        raise


# ============================================================
# REGISTRAR PAGO CON COMPROBANTE
# Transferencia => pendiente de verificación
# ============================================================

@router.post(
    "/registrar-con-comprobante",
    response_model=PagoOut,
    status_code=status.HTTP_201_CREATED,
)
async def registrar_pago_con_comprobante(
    pacienteid: int = Form(...),
    monto: float = Form(...),
    metodopago: str = Form(...),
    pacientepaqueteid: Optional[int] = Form(None),
    tratamientopacienteid: Optional[int] = Form(None),
    membresiagimnasioid: Optional[int] = Form(None),
    numerocomprobante: Optional[str] = Form(None),
    comprobante: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    _validar_destino_pago(
        pacientepaqueteid=pacientepaqueteid,
        tratamientopacienteid=tratamientopacienteid,
        membresiagimnasioid=membresiagimnasioid,
    )

    paciente = _validar_paciente(
        db=db,
        pacienteid=pacienteid,
        current_user=current_user,
        tratamientopacienteid=tratamientopacienteid,
        permitir_atencion_compartida=True,
    )

    if monto <= 0:
        raise HTTPException(
            status_code=400,
            detail="El monto debe ser mayor a 0.",
        )

    metodo = (metodopago or "").strip()

    if not metodo:
        raise HTTPException(
            status_code=400,
            detail="Seleccione un método de pago.",
        )

    paciente_paquete = _validar_paciente_paquete(
        db=db,
        pacienteid=pacienteid,
        pacientepaqueteid=pacientepaqueteid,
    )

    _validar_tratamiento_paciente(
        db=db,
        pacienteid=pacienteid,
        tratamientopacienteid=tratamientopacienteid,
    )

    membresia_gimnasio = _validar_membresia_gimnasio(
        db=db,
        pacienteid=pacienteid,
        membresiagimnasioid=membresiagimnasioid,
    )

    _validar_saldo_paquete(
        db=db,
        paciente_paquete=paciente_paquete,
        monto=monto,
    )

    _validar_saldo_membresia_gimnasio(
        db=db,
        membresia=membresia_gimnasio,
        monto=monto,
    )

    current_user_id = current_user.id

    paciente_ref = SimpleNamespace(
        id=paciente.id,
        consultorioid=paciente.consultorioid,
        nombres=paciente.nombres,
        apellidos=paciente.apellidos,
    )

    # Liberar conexión antes de subir imagen.
    db.rollback()
    db.close()

    comprobante_path = None
    estado_pago = _estado_pago_por_metodo(metodo)

    if _es_transferencia(metodo):
        if not numerocomprobante or not numerocomprobante.strip():
            raise HTTPException(
                status_code=400,
                detail="Ingrese el número de comprobante.",
            )

        if comprobante is None:
            raise HTTPException(
                status_code=400,
                detail="Debe subir la foto del comprobante.",
            )

        comprobante_path = await subir_comprobante_pago(
            comprobante,
            pacienteid,
        )

        estado_pago = 1

    else:
        numerocomprobante = None
        comprobante_path = None
        estado_pago = 2

    try:
        # Reconsultar usuario después del db.close().
        current_user_db = (
            db.query(Usuario)
            .filter(
                Usuario.id == current_user_id,
                Usuario.activo == True,
            )
            .first()
        )

        if not current_user_db:
            raise HTTPException(
                status_code=401,
                detail="Usuario no encontrado o inactivo.",
            )

        # Reconsultar entidades después del db.close().
        paciente_paquete_actual = _validar_paciente_paquete(
            db=db,
            pacienteid=pacienteid,
            pacientepaqueteid=pacientepaqueteid,
        )

        _validar_tratamiento_paciente(
            db=db,
            pacienteid=pacienteid,
            tratamientopacienteid=tratamientopacienteid,
        )

        membresia_gimnasio_actual = _validar_membresia_gimnasio(
            db=db,
            pacienteid=pacienteid,
            membresiagimnasioid=membresiagimnasioid,
        )

        # Revalidar saldo después del upload.
        _validar_saldo_paquete(
            db=db,
            paciente_paquete=paciente_paquete_actual,
            monto=monto,
        )

        _validar_saldo_membresia_gimnasio(
            db=db,
            membresia=membresia_gimnasio_actual,
            monto=monto,
        )

        nuevo_pago = Pago(
            pacienteid=pacienteid,
            pacientepaqueteid=pacientepaqueteid,
            tratamientopacienteid=tratamientopacienteid,
            membresiagimnasioid=membresiagimnasioid,
            monto=monto,
            metodopago=metodo,
            numerocomprobante=numerocomprobante.strip() if numerocomprobante else None,
            comprobanteurl=comprobante_path,
            estadopago=estado_pago,
            creado_por_id=current_user_db.id,
            verificado_por_id=None if estado_pago == 1 else current_user_db.id,
            fechapago=now_utc(),
            fecha_verificacion=None if estado_pago == 1 else now_utc(),
            motivo_rechazo=None,
        )

        db.add(nuevo_pago)
        db.flush()

        if estado_pago == 1:
            _notificar_pago_transferencia_pendiente(
                db=db,
                pago=nuevo_pago,
                paciente=paciente_ref,
                current_user=current_user_db,
            )

        respuesta = PagoOut.model_validate(nuevo_pago)

        db.commit()

        return respuesta

    except Exception:
        db.rollback()
        raise


# ============================================================
# ANULAR / VERIFICAR / RECHAZAR PAGO
# ============================================================

@router.put("/{pago_id}/anular", response_model=PagoOut)
def anular_pago(
    pago_id: int,
    data: PagoAnularRequest,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    if current_user.rol not in [1, 3]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tienes permisos para anular pagos.",
        )

    pago = db.query(Pago).filter(Pago.id == pago_id).first()

    if not pago:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Pago no encontrado.",
        )

    if pago.anulado:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este pago ya fue anulado.",
        )

    pago.anulado = True
    pago.anulado_por_id = current_user.id
    pago.fecha_anulacion = now_utc()
    pago.motivo_anulacion = data.motivo_anulacion.strip()

    db.commit()
    db.refresh(pago)

    return pago


@router.patch("/{pago_id}/verificar", response_model=PagoOut)
def verificar_pago(
    pago_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    pago, paciente = _obtener_pago_con_acceso(
        db=db,
        pago_id=pago_id,
        current_user=current_user,
    )

    pago.estadopago = 2
    pago.verificado_por_id = current_user.id
    pago.fecha_verificacion = now_utc()
    pago.motivo_rechazo = None

    _notificar_resultado_pago_transferencia(
        db=db,
        pago=pago,
        paciente=paciente,
        current_user=current_user,
        tipo="pago_transferencia_verificada",
    )

    db.commit()
    db.refresh(pago)

    return pago


@router.patch("/{pago_id}/rechazar", response_model=PagoOut)
def rechazar_pago(
    pago_id: int,
    motivo_rechazo: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    pago, paciente = _obtener_pago_con_acceso(
        db=db,
        pago_id=pago_id,
        current_user=current_user,
    )

    pago.estadopago = 3
    pago.verificado_por_id = current_user.id
    pago.fecha_verificacion = now_utc()
    pago.motivo_rechazo = (
        motivo_rechazo.strip()
        if motivo_rechazo and motivo_rechazo.strip()
        else None
    )

    _notificar_resultado_pago_transferencia(
        db=db,
        pago=pago,
        paciente=paciente,
        current_user=current_user,
        tipo="pago_transferencia_rechazada",
    )

    db.commit()
    db.refresh(pago)

    return pago