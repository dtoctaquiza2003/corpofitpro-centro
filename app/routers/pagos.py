from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.schemas.pago import (
    CuentaMembresiaGimnasioOut,
    CuentaPaqueteOut,
    CuentaTratamientoOut,
    PagoCreate,
    PagoOut,
    PagoSimpleOut,
    PagoAnularRequest
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
from ..services.supabase_storage import (
    crear_url_firmada_comprobante,
    subir_comprobante_pago,
)

router = APIRouter(prefix="/api/pagos", tags=["pagos"])


# ============================================================
# HELPERS
# ============================================================

def _es_transferencia(metodo: str) -> bool:
    return "transfer" in (metodo or "").strip().lower()


def _estado_pago_por_metodo(metodo: str) -> int:
    if _es_transferencia(metodo):
        return 1  # Pendiente
    return 2  # Verificado


def _estado_cuenta(total_generado: float, pagado: float, saldo: float) -> str:
    if total_generado <= 0 and pagado <= 0:
        return "SIN CARGOS"

    if pagado <= 0:
        return "PENDIENTE"

    if saldo > 0:
        return "PARCIAL"

    return "PAGADO"


def _validar_paciente(
    db: Session,
    pacienteid: int,
    current_user: Usuario,
) -> Paciente:
    paciente = db.query(Paciente).filter(Paciente.id == pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    validar_acceso_paciente_por_rol(paciente, current_user)

    return paciente


def _obtener_pago_con_acceso(
    db: Session,
    pago_id: int,
    current_user: Usuario,
) -> Pago:
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

    validar_acceso_paciente_por_rol(paciente, current_user)

    return pago


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

    query = db.query(Pago).filter(Pago.pacientepaqueteid == paciente_paquete.id)

    if excluir_pago_id is not None:
        query = query.filter(Pago.id != excluir_pago_id)

    pagos_actuales = query.all()

    total_pagado_verificado = sum(
        float(p.monto)
        for p in pagos_actuales
        if p.estadopago == 2
    )

    total_pendiente_verificacion = sum(
        float(p.monto)
        for p in pagos_actuales
        if p.estadopago == 1
    )

    precio_final = float(paciente_paquete.preciofinal)

    if total_pagado_verificado + total_pendiente_verificacion + float(monto) > precio_final:
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

    query = db.query(Pago).filter(Pago.membresiagimnasioid == membresia.id)

    if excluir_pago_id is not None:
        query = query.filter(Pago.id != excluir_pago_id)

    pagos_actuales = query.all()

    total_pagado_verificado = sum(
        float(p.monto)
        for p in pagos_actuales
        if p.estadopago == 2
    )

    total_pendiente_verificacion = sum(
        float(p.monto)
        for p in pagos_actuales
        if p.estadopago == 1
    )

    if total_pagado_verificado + total_pendiente_verificacion + float(monto) > precio:
        raise HTTPException(
            status_code=400,
            detail="El abono supera el saldo pendiente de la membresía de gimnasio.",
        )


def now_ecuador() -> datetime:
    return datetime.now(timezone(timedelta(hours=-5)))


def _nombre_paciente(paciente: Paciente) -> str:
    return f"{paciente.nombres} {paciente.apellidos}".strip()


def _obtener_usuarios_verificadores_pago(
    db: Session,
    paciente: Paciente,
    current_user: Usuario,
) -> list[Usuario]:
    usuarios: list[Usuario] = []
    usuarios_ids = set()

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
        if secretario.id != current_user.id and secretario.id not in usuarios_ids:
            usuarios.append(secretario)
            usuarios_ids.add(secretario.id)

    jefes = (
        db.query(Usuario)
        .filter(
            Usuario.rol == 3,
            Usuario.activo == True,
        )
        .all()
    )

    for jefe in jefes:
        if jefe.id != current_user.id and jefe.id not in usuarios_ids:
            usuarios.append(jefe)
            usuarios_ids.add(jefe.id)

    return usuarios


def _notificar_pago_transferencia_pendiente(
    db: Session,
    pago: Pago,
    paciente: Paciente,
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
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
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
            Paciente.consultorioid == current_user.consultorioid
        )

    elif current_user.rol == 3:
        pass

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    return query.order_by(Pago.fechapago.desc()).all()


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

    resultado = []

    for paciente_paquete, paciente, paquete in asignaciones:
        pagos = (
            db.query(Pago)
            .filter(Pago.pacientepaqueteid == paciente_paquete.id)
            .order_by(Pago.fechapago.asc())
            .all()
        )

        precio_final = float(paciente_paquete.preciofinal)

        pagado_verificado = sum(
            float(pago.monto)
            for pago in pagos
            if pago.estadopago == 2
        )

        pendiente_verificacion = sum(
            float(pago.monto)
            for pago in pagos
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
                    )
                    for pago in pagos
                ],
            )
        )

    return resultado


# ============================================================
# CUENTAS POR TRATAMIENTO - NUEVO SISTEMA
# ============================================================

@router.get("/cuentas-tratamientos", response_model=List[CuentaTratamientoOut])
def listar_cuentas_tratamientos(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(TratamientoPaciente, Paciente)
        .options(joinedload(TratamientoPaciente.tipo_terapia))
        .join(Paciente, Paciente.id == TratamientoPaciente.pacienteid)
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

    tratamientos = (
        query.order_by(
            TratamientoPaciente.activo.desc(),
            TratamientoPaciente.fechainicio.desc(),
            TratamientoPaciente.id.desc(),
        )
        .all()
    )

    if not tratamientos:
        return []

    tratamiento_ids = [tratamiento.id for tratamiento, _ in tratamientos]

    sesiones_rows = (
        db.query(
            SesionTerapia.tratamientopacienteid,
            func.count(SesionTerapia.id),
        )
        .filter(
            SesionTerapia.tratamientopacienteid.in_(tratamiento_ids),
            SesionTerapia.horasalida != None,
        )
        .group_by(SesionTerapia.tratamientopacienteid)
        .all()
    )

    sesiones_por_tratamiento = {
        tratamiento_id: int(total or 0)
        for tratamiento_id, total in sesiones_rows
    }

    pagos_rows = (
        db.query(Pago)
        .filter(Pago.tratamientopacienteid.in_(tratamiento_ids))
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

        pagos = pagos_por_tratamiento.get(tratamiento.id, [])

        pagos_no_anulados = [
            pago for pago in pagos if not bool(pago.anulado)
        ]

        pagado_verificado = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 2
        )

        pendiente_verificacion = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 1
        )

        saldo_real = total_generado - pagado_verificado
        saldo = max(saldo_real, 0)
        saldo_favor = max(pagado_verificado - total_generado, 0)

        nombre_tipo_terapia = None

        if tratamiento.tipo_terapia:
            nombre_tipo_terapia = tratamiento.tipo_terapia.nombre

        resultado.append(
            CuentaTratamientoOut(
                tratamientopacienteid=tratamiento.id,
                pacienteid=paciente.id,
                paciente=f"{paciente.nombres} {paciente.apellidos}",
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

                        # IMPORTANTE PARA QUE FLUTTER LO MUESTRE BIEN
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
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = (
        db.query(MembresiaGimnasio, Paciente)
        .join(Paciente, Paciente.id == MembresiaGimnasio.pacienteid)
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

    membresias = (
        query.order_by(
            MembresiaGimnasio.activo.desc(),
            MembresiaGimnasio.fechainicio.desc(),
            MembresiaGimnasio.id.desc(),
        )
        .all()
    )

    if not membresias:
        return []

    membresia_ids = [membresia.id for membresia, _ in membresias]

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
            pago for pago in pagos if not bool(pago.anulado)
        ]

        pagado_verificado = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 2
        )

        pendiente_verificacion = sum(
            float(pago.monto or 0)
            for pago in pagos_no_anulados
            if pago.estadopago == 1
        )

        saldo_real = precio - pagado_verificado
        saldo = max(saldo_real, 0)
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
# URL FIRMADA DEL COMPROBANTE
# ============================================================

@router.get("/{pago_id}/comprobante-url")
def obtener_url_comprobante(
    pago_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    pago = _obtener_pago_con_acceso(
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
    data["fecha_verificacion"] = now_ecuador()
    data["motivo_rechazo"] = None

    nuevo_pago = Pago(**data)

    db.add(nuevo_pago)
    db.commit()
    db.refresh(nuevo_pago)

    return nuevo_pago


# ============================================================
# REGISTRAR PAGO CON COMPROBANTE
# Transferencia => pendiente de verificación
# También permite efectivo/tarjeta sin archivo si lo usas desde el mismo form
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
        creado_por_id=current_user.id,
        verificado_por_id=None if estado_pago == 1 else current_user.id,
        fecha_verificacion=None if estado_pago == 1 else now_ecuador(),
        motivo_rechazo=None,
    )

    db.add(nuevo_pago)
    db.flush()

    if estado_pago == 1:
        _notificar_pago_transferencia_pendiente(
            db=db,
            pago=nuevo_pago,
            paciente=paciente,
            current_user=current_user,
        )

    db.commit()
    db.refresh(nuevo_pago)

    return nuevo_pago


# ============================================================
# VERIFICAR / RECHAZAR PAGO
# ============================================================

@router.put("/{pago_id}/anular", response_model=PagoOut)
def anular_pago(
    pago_id: int,
    data: PagoAnularRequest,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    # Solo jefe o secretario
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
    pago.fecha_anulacion = datetime.now(timezone.utc)
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
    pago = _obtener_pago_con_acceso(
        db=db,
        pago_id=pago_id,
        current_user=current_user,
    )

    paciente = db.query(Paciente).filter(Paciente.id == pago.pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente del pago no encontrado.",
        )

    pago.estadopago = 2
    pago.verificado_por_id = current_user.id
    pago.fecha_verificacion = now_ecuador()
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
    pago = _obtener_pago_con_acceso(
        db=db,
        pago_id=pago_id,
        current_user=current_user,
    )

    paciente = db.query(Paciente).filter(Paciente.id == pago.pacienteid).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente del pago no encontrado.",
        )

    pago.estadopago = 3
    pago.verificado_por_id = current_user.id
    pago.fecha_verificacion = now_ecuador()
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