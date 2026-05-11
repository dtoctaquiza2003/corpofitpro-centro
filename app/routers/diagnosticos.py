from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session, joinedload

from ..auth.dependencies import get_current_secretary, get_current_user
from ..auth.permissions import validar_acceso_paciente_por_rol
from ..dependencies.db import get_db
from ..models.diagnostico import Diagnostico
from ..models.paciente import Paciente
from ..models.tratamiento_paciente import TratamientoPaciente
from ..models.usuario import Usuario
from ..schemas.diagnostico import DiagnosticoCreate, DiagnosticoOut, DiagnosticoUpdate
from ..models.tipo_terapia import TipoTerapia
from ..services.notificacion_service import crear_notificacion_usuario

router = APIRouter(prefix="/api/diagnosticos", tags=["diagnosticos"])


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


def _obtener_diagnostico_con_acceso(
    db: Session,
    diagnostico_id: int,
    current_user: Usuario,
) -> Diagnostico:
    diagnostico = (
        db.query(Diagnostico)
        .options(joinedload(Diagnostico.tratamientos))
        .filter(Diagnostico.id == diagnostico_id)
        .first()
    )

    if not diagnostico:
        raise HTTPException(
            status_code=404,
            detail="Diagnóstico no encontrado",
        )

    _obtener_paciente_con_acceso(
        db=db,
        paciente_id=diagnostico.pacienteid,
        current_user=current_user,
    )

    return diagnostico

def _nombre_paciente(paciente: Paciente) -> str:
    return f"{paciente.nombres} {paciente.apellidos}".strip()


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


@router.get("/paciente/{paciente_id}", response_model=List[DiagnosticoOut])
def listar_diagnosticos(
    paciente_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    _obtener_paciente_con_acceso(
        db=db,
        paciente_id=paciente_id,
        current_user=current_user,
    )

    diagnosticos = (
        db.query(Diagnostico)
        .options(joinedload(Diagnostico.tratamientos))
        .filter(Diagnostico.pacienteid == paciente_id)
        .order_by(Diagnostico.fechadiagnostico.desc())
        .all()
    )

    return diagnosticos


@router.post("/", response_model=DiagnosticoOut, status_code=status.HTTP_201_CREATED)
def crear_diagnostico(
    diagnostico: DiagnosticoCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    paciente = _obtener_paciente_con_acceso(
        db=db,
        paciente_id=diagnostico.pacienteid,
        current_user=current_user,
    )

    nuevo_diagnostico = Diagnostico(
        pacienteid=diagnostico.pacienteid,
        diagnostico=diagnostico.diagnostico,
        fechadiagnostico=diagnostico.fechadiagnostico,
        activo=diagnostico.activo,
        notas=diagnostico.notas,
    )

    db.add(nuevo_diagnostico)
    db.flush()

    tratamientos_creados_ids = []
    tratamientos_creados_nombres = []

    for tratamiento_data in diagnostico.tratamientos:
        datos_tratamiento = tratamiento_data.model_dump()

        tipo_terapia = _obtener_tipo_terapia(
            db=db,
            tipo_terapia_id=datos_tratamiento.get("tipoterapiaid"),
        )

        if tipo_terapia:
            precio_oficial = float(tipo_terapia.precio_sesion)

            precio_aplicado = (
                datos_tratamiento.get("precio_sesion_aplicado")
                if datos_tratamiento.get("precio_sesion_aplicado") is not None
                else precio_oficial
            )

            _validar_precio_especial(
                precio_oficial=precio_oficial,
                precio_aplicado=precio_aplicado,
                motivo=datos_tratamiento.get("motivo_precio_especial"),
            )

            datos_tratamiento["precio_sesion_oficial"] = precio_oficial
            datos_tratamiento["precio_sesion_aplicado"] = precio_aplicado

            if not datos_tratamiento.get("tipotratamiento"):
                datos_tratamiento["tipotratamiento"] = tipo_terapia.nombre

        else:
            if not datos_tratamiento.get("tipotratamiento"):
                raise HTTPException(
                    status_code=400,
                    detail="Debe ingresar el tratamiento o seleccionar un tipo de terapia.",
                )

        datos_tratamiento["pacienteid"] = diagnostico.pacienteid
        datos_tratamiento["diagnosticoid"] = nuevo_diagnostico.id

        tratamiento = TratamientoPaciente(**datos_tratamiento)

        db.add(tratamiento)
        db.flush()

        tratamientos_creados_ids.append(tratamiento.id)
        tratamientos_creados_nombres.append(tratamiento.tipotratamiento)

    if paciente.terapeutaasignadoid:
        nombre_paciente = _nombre_paciente(paciente)

        crear_notificacion_usuario(
            db=db,
            usuarioid=paciente.terapeutaasignadoid,
            titulo="Nuevo diagnóstico registrado",
            mensaje=f"Se registró un nuevo diagnóstico con tratamiento para {nombre_paciente}.",
            tipo="diagnostico_creado",
            referencia_tipo="diagnostico",
            referencia_id=nuevo_diagnostico.id,
            data={
                "paciente_id": paciente.id,
                "diagnostico_id": nuevo_diagnostico.id,
                "tratamiento_ids": tratamientos_creados_ids,
                "tratamientos": tratamientos_creados_nombres,
                "consultorioid": paciente.consultorioid,
                "creado_por_id": current_user.id,
                "actualizar": [
                    "diagnosticos",
                    "tratamientos",
                    "pacientes",
                    "dashboard",
                    "notificaciones",
                ],
            },
        )

    db.commit()

    resultado = (
        db.query(Diagnostico)
        .options(joinedload(Diagnostico.tratamientos))
        .filter(Diagnostico.id == nuevo_diagnostico.id)
        .first()
    )

    return resultado


@router.put("/{diagnostico_id}", response_model=DiagnosticoOut)
def actualizar_diagnostico(
    diagnostico_id: int,
    diagnostico: DiagnosticoUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    db_diagnostico = _obtener_diagnostico_con_acceso(
        db=db,
        diagnostico_id=diagnostico_id,
        current_user=current_user,
    )

    datos = diagnostico.model_dump(exclude_unset=True)

    if "pacienteid" in datos:
        _obtener_paciente_con_acceso(
            db=db,
            paciente_id=datos["pacienteid"],
            current_user=current_user,
        )

    for key, value in datos.items():
        setattr(db_diagnostico, key, value)

    db.commit()

    resultado = (
        db.query(Diagnostico)
        .options(joinedload(Diagnostico.tratamientos))
        .filter(Diagnostico.id == db_diagnostico.id)
        .first()
    )

    return resultado


@router.delete("/{diagnostico_id}")
def eliminar_diagnostico(
    diagnostico_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_secretary),
):
    db_diagnostico = _obtener_diagnostico_con_acceso(
        db=db,
        diagnostico_id=diagnostico_id,
        current_user=current_user,
    )

    db_diagnostico.activo = False

    db.commit()

    return {
        "ok": True,
        "message": "Diagnóstico desactivado correctamente",
    }