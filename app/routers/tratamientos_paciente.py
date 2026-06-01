from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from ..auth.dependencies import get_current_secretary, get_current_user
from ..auth.permissions import (
    TIPO_CREAR_TRATAMIENTOS,
    permiso_temporal_activo,
    validar_acceso_paciente_por_rol,
)
from ..dependencies.db import get_db
from ..models.diagnostico import Diagnostico
from ..models.paciente import Paciente
from ..models.tipo_terapia import TipoTerapia
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..schemas.tratamiento_paciente import (
    TratamientoPacienteCreate,
    TratamientoPacienteOut,
    TratamientoPacienteUpdate,
)
from ..services.notificacion_service import crear_notificacion_usuario

router = APIRouter(
    prefix="/api/tratamientos-paciente",
    tags=["tratamientos-paciente"],
)


def _validar_paciente(
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

    validar_acceso_paciente_por_rol(
        paciente=paciente,
        current_user=current_user,
        db=db,
    )

    return paciente


def _validar_diagnostico(
    db: Session,
    diagnostico_id: int | None,
    paciente_id: int,
):
    if diagnostico_id is None:
        return None

    diagnostico = (
        db.query(Diagnostico)
        .filter(
            Diagnostico.id == diagnostico_id,
            Diagnostico.pacienteid == paciente_id,
        )
        .first()
    )

    if not diagnostico:
        raise HTTPException(
            status_code=400,
            detail="El diagnóstico no existe o no pertenece a este paciente",
        )

    return diagnostico


def _obtener_tipo_terapia(
    db: Session,
    tipo_terapia_id: int | None,
) -> TipoTerapia | None:
    if tipo_terapia_id is None:
        return None

    tipo = (
        db.query(TipoTerapia)
        .filter(
            TipoTerapia.id == tipo_terapia_id,
            TipoTerapia.activo == True,
        )
        .first()
    )

    if not tipo:
        raise HTTPException(
            status_code=400,
            detail="El tipo de terapia no existe o está inactivo.",
        )

    return tipo


def _validar_precio_especial(
    precio_oficial: float | None,
    precio_aplicado: float | None,
    motivo: str | None,
):
    if precio_oficial is None or precio_aplicado is None:
        return

    if float(precio_aplicado) != float(precio_oficial):
        if not motivo or not motivo.strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Debe ingresar un motivo cuando el precio aplicado "
                    "es diferente al precio oficial."
                ),
            )


def _nombre_paciente(paciente: Paciente) -> str:
    return f"{paciente.nombres} {paciente.apellidos}".strip()


def _validar_permiso_crear_tratamiento(
    db: Session,
    current_user: Usuario,
    paciente: Paciente,
) -> None:
    """
    Permite crear tratamientos a jefe/secretario normalmente y a terapeutas
    únicamente cuando tienen el permiso temporal activo.

    La validación de acceso al paciente ya se hace antes con
    validar_acceso_paciente_por_rol(), así el permiso no abre pacientes ajenos.
    """

    if current_user.rol in (1, 3):
        return

    if current_user.rol == 2:
        permiso = permiso_temporal_activo(
            db=db,
            usuario=current_user,
            tipo_permiso=TIPO_CREAR_TRATAMIENTOS,
        )

        if permiso:
            return

        raise HTTPException(
            status_code=403,
            detail=(
                "No tienes permiso activo para crear tratamientos. "
                "Solicita autorización temporal al jefe o secretario."
            ),
        )

    raise HTTPException(
        status_code=403,
        detail="No autorizado para crear tratamientos.",
    )

@router.get("/paciente/{paciente_id}", response_model=List[TratamientoPacienteOut])
def listar_tratamientos_paciente(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _validar_paciente(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    tratamientos = (
        db.query(TratamientoPaciente)
        .options(
            joinedload(TratamientoPaciente.diagnostico),
            joinedload(TratamientoPaciente.tipo_terapia),
        )
        .filter(TratamientoPaciente.pacienteid == paciente_id)
        .order_by(
            TratamientoPaciente.activo.desc(),
            TratamientoPaciente.fechainicio.desc(),
            TratamientoPaciente.id.desc(),
        )
        .all()
    )

    return tratamientos


@router.post("/", response_model=TratamientoPacienteOut, status_code=status.HTTP_201_CREATED)
def crear_tratamiento_paciente(
    tratamiento: TratamientoPacienteCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    paciente = _validar_paciente(
        db=db,
        paciente_id=tratamiento.pacienteid,
        current_user=current_user,
    )

    _validar_permiso_crear_tratamiento(
        db=db,
        current_user=current_user,
        paciente=paciente,
    )

    _validar_diagnostico(
        db=db,
        diagnostico_id=tratamiento.diagnosticoid,
        paciente_id=tratamiento.pacienteid,
    )

    tipo_terapia = _obtener_tipo_terapia(
        db=db,
        tipo_terapia_id=tratamiento.tipoterapiaid,
    )

    datos = tratamiento.model_dump()

    if tipo_terapia:
        precio_oficial = float(tipo_terapia.precio_sesion)

        precio_aplicado = (
            datos.get("precio_sesion_aplicado")
            if datos.get("precio_sesion_aplicado") is not None
            else precio_oficial
        )

        _validar_precio_especial(
            precio_oficial=precio_oficial,
            precio_aplicado=precio_aplicado,
            motivo=datos.get("motivo_precio_especial"),
        )

        datos["precio_sesion_oficial"] = precio_oficial
        datos["precio_sesion_aplicado"] = precio_aplicado

        if not datos.get("tipotratamiento"):
            datos["tipotratamiento"] = tipo_terapia.nombre

    else:
        if not datos.get("tipotratamiento"):
            raise HTTPException(
                status_code=400,
                detail="Debe ingresar el tratamiento o seleccionar un tipo de terapia.",
            )

    nuevo = TratamientoPaciente(**datos)

    db.add(nuevo)
    db.flush()

    if paciente.terapeutaasignadoid:
        nombre_paciente = _nombre_paciente(paciente)

        crear_notificacion_usuario(
            db=db,
            usuarioid=paciente.terapeutaasignadoid,
            titulo="Nuevo tratamiento asignado",
            mensaje=f"Se asignó el tratamiento {nuevo.tipotratamiento} a {nombre_paciente}.",
            tipo="tratamiento_creado",
            referencia_tipo="tratamiento",
            referencia_id=nuevo.id,
            data={
                "paciente_id": paciente.id,
                "tratamiento_id": nuevo.id,
                "diagnostico_id": nuevo.diagnosticoid,
                "consultorioid": paciente.consultorioid,
                "creado_por_id": current_user.id,
                "actualizar": [
                    "tratamientos",
                    "diagnosticos",
                    "pacientes",
                    "dashboard",
                    "notificaciones",
                ],
            },
        )

    db.commit()
    db.refresh(nuevo)

    resultado = (
        db.query(TratamientoPaciente)
        .options(
            joinedload(TratamientoPaciente.diagnostico),
            joinedload(TratamientoPaciente.tipo_terapia),
        )
        .filter(TratamientoPaciente.id == nuevo.id)
        .first()
    )

    return resultado


@router.put("/{tratamiento_id}", response_model=TratamientoPacienteOut)
def actualizar_tratamiento_paciente(
    tratamiento_id: int,
    tratamiento: TratamientoPacienteUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    db_tratamiento = (
        db.query(TratamientoPaciente)
        .filter(TratamientoPaciente.id == tratamiento_id)
        .first()
    )

    if not db_tratamiento:
        raise HTTPException(
            status_code=404,
            detail="Tratamiento no encontrado",
        )

    _validar_paciente(
        db=db,
        paciente_id=db_tratamiento.pacienteid,
        current_user=current_user,
    )

    datos = tratamiento.model_dump(exclude_unset=True)

    pacienteid = datos.get("pacienteid", db_tratamiento.pacienteid)

    _validar_paciente(
        db=db,
        paciente_id=pacienteid,
        current_user=current_user,
    )

    diagnosticoid = datos.get("diagnosticoid", db_tratamiento.diagnosticoid)

    _validar_diagnostico(
        db=db,
        diagnostico_id=diagnosticoid,
        paciente_id=pacienteid,
    )

    if "tipoterapiaid" in datos:
        tipo_terapia = _obtener_tipo_terapia(
            db=db,
            tipo_terapia_id=datos.get("tipoterapiaid"),
        )

        if tipo_terapia:
            precio_oficial = float(tipo_terapia.precio_sesion)

            precio_aplicado = datos.get(
                "precio_sesion_aplicado",
                precio_oficial,
            )

            _validar_precio_especial(
                precio_oficial=precio_oficial,
                precio_aplicado=precio_aplicado,
                motivo=datos.get(
                    "motivo_precio_especial",
                    db_tratamiento.motivo_precio_especial,
                ),
            )

            datos["precio_sesion_oficial"] = precio_oficial
            datos["precio_sesion_aplicado"] = precio_aplicado

            if not datos.get("tipotratamiento"):
                datos["tipotratamiento"] = tipo_terapia.nombre

    elif "precio_sesion_aplicado" in datos:
        precio_oficial = (
            float(db_tratamiento.precio_sesion_oficial)
            if db_tratamiento.precio_sesion_oficial is not None
            else None
        )

        _validar_precio_especial(
            precio_oficial=precio_oficial,
            precio_aplicado=datos.get("precio_sesion_aplicado"),
            motivo=datos.get(
                "motivo_precio_especial",
                db_tratamiento.motivo_precio_especial,
            ),
        )

    for key, value in datos.items():
        setattr(db_tratamiento, key, value)

    db.commit()
    db.refresh(db_tratamiento)

    resultado = (
        db.query(TratamientoPaciente)
        .options(
            joinedload(TratamientoPaciente.diagnostico),
            joinedload(TratamientoPaciente.tipo_terapia),
        )
        .filter(TratamientoPaciente.id == db_tratamiento.id)
        .first()
    )

    return resultado


@router.delete("/{tratamiento_id}")
def eliminar_tratamiento_paciente(
    tratamiento_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    db_tratamiento = (
        db.query(TratamientoPaciente)
        .filter(TratamientoPaciente.id == tratamiento_id)
        .first()
    )

    if not db_tratamiento:
        raise HTTPException(
            status_code=404,
            detail="Tratamiento no encontrado",
        )

    _validar_paciente(
        db=db,
        paciente_id=db_tratamiento.pacienteid,
        current_user=current_user,
    )

    db_tratamiento.activo = False

    db.commit()

    return {
        "ok": True,
        "message": "Tratamiento desactivado correctamente",
    }