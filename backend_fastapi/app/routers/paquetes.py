from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends, status
from sqlalchemy.orm import Session

from app.schemas.paquete import (
    PacientePaqueteCreate,
    PaqueteCreate,
    PaqueteEstadoUpdate,
    PaqueteOut,
    PaqueteUpdate,
)

from ..auth.dependencies import get_current_jefe, get_current_secretary, get_current_user
from ..auth.permissions import validar_acceso_paciente_por_rol
from ..dependencies.db import get_db
from ..models.paciente import Paciente
from ..models.paciente_paquete import PacientePaquete
from ..models.pago import Pago
from ..models.paquete import Paquete
from ..models.usuario import Usuario
from ..services.supabase_storage import subir_comprobante_pago

router = APIRouter(prefix="/api/paquetes", tags=["paquetes"])


def validar_configuracion_paquete(
    nombre: str,
    cantidad_sesiones: int,
    precio_oficial: float,
    duracion_dias: int | None,
):
    if not nombre or not nombre.strip():
        raise HTTPException(
            status_code=400,
            detail="El nombre del paquete es obligatorio",
        )

    if precio_oficial <= 0:
        raise HTTPException(
            status_code=400,
            detail="El precio debe ser mayor a 0",
        )

    if duracion_dias is None and cantidad_sesiones <= 0:
        raise HTTPException(
            status_code=400,
            detail="Los paquetes de fisioterapia deben tener al menos una sesión",
        )


def calcular_fecha_expiracion(paquete: Paquete):
    fecha_asignacion = datetime.now().date()

    if paquete.duracion_dias is None:
        return fecha_asignacion, None

    return fecha_asignacion, fecha_asignacion + timedelta(days=paquete.duracion_dias)


def _obtener_paciente_con_acceso(
    db: Session,
    paciente_id: int,
    current_user: Usuario,
) -> Paciente:
    paciente = db.query(Paciente).filter(Paciente.id == paciente_id).first()

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente no encontrado",
        )

    validar_acceso_paciente_por_rol(paciente, current_user)

    return paciente


def _obtener_paquete(
    db: Session,
    paquete_id: int,
) -> Paquete:
    paquete = db.query(Paquete).filter(Paquete.id == paquete_id).first()

    if not paquete:
        raise HTTPException(
            status_code=404,
            detail="Paquete no encontrado",
        )

    return paquete


def _validar_paquete_asignable(
    paquete: Paquete,
    sesionescontratadas: int,
):
    if not paquete.activo:
        raise HTTPException(
            status_code=400,
            detail="No se puede asignar un paquete inactivo",
        )

    if paquete.duracion_dias is None and sesionescontratadas <= 0:
        raise HTTPException(
            status_code=400,
            detail="Este paquete debe tener sesiones contratadas",
        )


@router.get("/", response_model=List[PaqueteOut])
def listar_paquetes_activos(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    if current_user.rol not in (1, 2, 3):
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    return (
        db.query(Paquete)
        .filter(Paquete.activo == True)
        .order_by(Paquete.id.asc())
        .all()
    )


@router.get("/catalogo", response_model=List[PaqueteOut])
def listar_catalogo_paquetes(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    return db.query(Paquete).order_by(Paquete.id.desc()).all()


@router.post("/", response_model=PaqueteOut, status_code=status.HTTP_201_CREATED)
def crear_paquete(
    paquete: PaqueteCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    datos = paquete.model_dump()

    validar_configuracion_paquete(
        nombre=datos["nombre"],
        cantidad_sesiones=datos["cantidad_sesiones"],
        precio_oficial=datos["precio_oficial"],
        duracion_dias=datos["duracion_dias"],
    )

    nuevo = Paquete(**datos)

    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)

    return nuevo


@router.put("/{paquete_id}", response_model=PaqueteOut)
def actualizar_paquete(
    paquete_id: int,
    paquete: PaqueteUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    db_paquete = _obtener_paquete(
        db=db,
        paquete_id=paquete_id,
    )

    datos = paquete.model_dump(exclude_unset=True)

    nombre = datos.get("nombre", db_paquete.nombre)
    cantidad_sesiones = datos.get(
        "cantidad_sesiones",
        db_paquete.cantidad_sesiones,
    )
    precio_oficial = datos.get(
        "precio_oficial",
        db_paquete.precio_oficial,
    )
    duracion_dias = datos.get(
        "duracion_dias",
        db_paquete.duracion_dias,
    )

    validar_configuracion_paquete(
        nombre=nombre,
        cantidad_sesiones=cantidad_sesiones or 0,
        precio_oficial=float(precio_oficial),
        duracion_dias=duracion_dias,
    )

    for key, value in datos.items():
        setattr(db_paquete, key, value)

    db.commit()
    db.refresh(db_paquete)

    return db_paquete


@router.patch("/{paquete_id}/estado", response_model=PaqueteOut)
def cambiar_estado_paquete(
    paquete_id: int,
    estado: PaqueteEstadoUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    db_paquete = _obtener_paquete(
        db=db,
        paquete_id=paquete_id,
    )

    db_paquete.activo = estado.activo

    db.commit()
    db.refresh(db_paquete)

    return db_paquete


@router.delete("/{paquete_id}", response_model=PaqueteOut)
def desactivar_paquete(
    paquete_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    db_paquete = _obtener_paquete(
        db=db,
        paquete_id=paquete_id,
    )

    db_paquete.activo = False

    db.commit()
    db.refresh(db_paquete)

    return db_paquete


@router.post("/asignar", status_code=status.HTTP_201_CREATED)
def asignar_paquete(
    asignacion: PacientePaqueteCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    _obtener_paciente_con_acceso(
        db=db,
        paciente_id=asignacion.pacienteid,
        current_user=current_user,
    )

    paquete = _obtener_paquete(
        db=db,
        paquete_id=asignacion.paqueteid,
    )

    _validar_paquete_asignable(
        paquete=paquete,
        sesionescontratadas=asignacion.sesionescontratadas,
    )

    if asignacion.preciofinal <= 0:
        raise HTTPException(
            status_code=400,
            detail="El precio final debe ser mayor a 0.",
        )

    fecha_asignacion, fecha_expiracion = calcular_fecha_expiracion(paquete)

    nueva_asignacion = PacientePaquete(
        pacienteid=asignacion.pacienteid,
        paqueteid=asignacion.paqueteid,
        preciofinal=asignacion.preciofinal,
        sesionescontratadas=asignacion.sesionescontratadas,
        sesionesusadas=0,
        fechaasignacion=fecha_asignacion,
        fechaexpiracion=fecha_expiracion,
        estado="ACTIVO",
    )

    db.add(nueva_asignacion)
    db.commit()
    db.refresh(nueva_asignacion)

    return {
        "id": nueva_asignacion.id,
        "fechaexpiracion": nueva_asignacion.fechaexpiracion,
        "message": "Paquete asignado correctamente",
    }


@router.post("/asignar-con-pago", status_code=status.HTTP_201_CREATED)
async def asignar_paquete_con_pago(
    pacienteid: int = Form(...),
    paqueteid: int = Form(...),
    preciofinal: float = Form(...),
    sesionescontratadas: int = Form(...),
    monto: float = Form(0),
    metodopago: Optional[str] = Form(None),
    numerocomprobante: Optional[str] = Form(None),
    comprobante: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    _obtener_paciente_con_acceso(
        db=db,
        paciente_id=pacienteid,
        current_user=current_user,
    )

    paquete = _obtener_paquete(
        db=db,
        paquete_id=paqueteid,
    )

    _validar_paquete_asignable(
        paquete=paquete,
        sesionescontratadas=sesionescontratadas,
    )

    if preciofinal <= 0:
        raise HTTPException(
            status_code=400,
            detail="El precio final debe ser mayor a 0.",
        )

    if monto < 0:
        raise HTTPException(
            status_code=400,
            detail="El monto no puede ser negativo.",
        )

    if monto > preciofinal:
        raise HTTPException(
            status_code=400,
            detail="El pago inicial no puede superar el precio final.",
        )

    fecha_asignacion, fecha_expiracion = calcular_fecha_expiracion(paquete)

    nueva_asignacion = PacientePaquete(
        pacienteid=pacienteid,
        paqueteid=paqueteid,
        preciofinal=preciofinal,
        sesionescontratadas=sesionescontratadas,
        sesionesusadas=0,
        fechaasignacion=fecha_asignacion,
        fechaexpiracion=fecha_expiracion,
        estado="ACTIVO",
    )

    db.add(nueva_asignacion)
    db.flush()

    pago_creado = None
    comprobante_path = None

    if monto > 0:
        if not metodopago or not metodopago.strip():
            raise HTTPException(
                status_code=400,
                detail="Seleccione un método de pago.",
            )

        metodo = metodopago.strip()
        es_transferencia = "transfer" in metodo.lower()

        if es_transferencia:
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

        pago_creado = Pago(
            pacienteid=pacienteid,
            pacientepaqueteid=nueva_asignacion.id,
            monto=monto,
            metodopago=metodo,
            numerocomprobante=(
                numerocomprobante.strip()
                if numerocomprobante
                else None
            ),
            comprobanteurl=comprobante_path,
            estadopago=estado_pago,
        )

        db.add(pago_creado)

    db.commit()
    db.refresh(nueva_asignacion)

    saldo = preciofinal - monto

    return {
        "message": "Paquete asignado correctamente.",
        "pacientepaqueteid": nueva_asignacion.id,
        "pacienteid": pacienteid,
        "paqueteid": paqueteid,
        "preciofinal": preciofinal,
        "monto": monto,
        "saldo": saldo,
        "pago_id": pago_creado.id if pago_creado else None,
        "numerocomprobante": (
            pago_creado.numerocomprobante
            if pago_creado
            else None
        ),
        "comprobanteurl": (
            pago_creado.comprobanteurl
            if pago_creado
            else None
        ),
        "estadopago": pago_creado.estadopago if pago_creado else None,
    }