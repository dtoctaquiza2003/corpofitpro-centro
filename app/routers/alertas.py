from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_user
from ..auth.permissions import (
    validar_acceso_paciente_por_rol,
    validar_consultorio_secretario,
)
from ..dependencies.db import get_db
from ..models.alerta import Alerta
from ..models.paciente import Paciente
from ..models.sesion_terapia import SesionTerapia
from ..models.usuario import Usuario
from ..schemas.alerta import AlertaOut
from ..utils.fechas import to_ecuador

router = APIRouter(prefix="/api/alertas", tags=["alertas"])


def _nombre_completo_persona(obj) -> str:
    nombres = (getattr(obj, "nombres", None) or "").strip()
    apellidos = (getattr(obj, "apellidos", None) or "").strip()
    nombre = f"{nombres} {apellidos}".strip()
    return nombre or "No registrado"


def _tipo_alerta_en_espanol(tipo: str | None) -> str:
    tipo_normalizado = (tipo or "").strip().lower()

    return {
        "high_pain": "Dolor crítico",
        "pain_no_reduction": "Dolor sin reducción",
        "pain_increase": "Aumento de dolor",
        "critical": "Crítica",
        "critical_pain": "Dolor crítico",
    }.get(tipo_normalizado, "Alerta clínica")


def _buscar_sesion_relacionada_alerta(
    db: Session,
    alerta: Alerta,
) -> SesionTerapia | None:
    """
    Las alertas antiguas no guardaban sesionid/terapeutaid.
    Para mostrar quién atendió, buscamos primero una sesión del mismo paciente
    en la fecha de la alerta y, si no existe, usamos la última sesión registrada
    del paciente como respaldo.
    """
    if not alerta.paciente_id:
        return None

    base_query = db.query(SesionTerapia).filter(
        SesionTerapia.pacienteid == alerta.paciente_id,
    )

    fecha_alerta = None
    try:
        fecha_alerta = to_ecuador(alerta.fecha).date() if alerta.fecha else None
    except Exception:
        fecha_alerta = None

    if fecha_alerta is not None:
        sesion_misma_fecha = (
            base_query
            .filter(SesionTerapia.fecha == fecha_alerta)
            .order_by(
                SesionTerapia.horasalida.desc(),
                SesionTerapia.horaingreso.desc(),
                SesionTerapia.id.desc(),
            )
            .first()
        )

        if sesion_misma_fecha:
            return sesion_misma_fecha

    return (
        base_query
        .order_by(
            SesionTerapia.fecha.desc(),
            SesionTerapia.horasalida.desc(),
            SesionTerapia.horaingreso.desc(),
            SesionTerapia.id.desc(),
        )
        .first()
    )


def _alerta_a_response(
    db: Session,
    alerta: Alerta,
    paciente: Paciente | None = None,
) -> dict:
    if paciente is None and alerta.paciente_id:
        paciente = (
            db.query(Paciente)
            .filter(Paciente.id == alerta.paciente_id)
            .first()
        )

    sesion = _buscar_sesion_relacionada_alerta(db, alerta)
    terapeuta = None

    if sesion and sesion.terapeutaid:
        terapeuta = (
            db.query(Usuario)
            .filter(Usuario.id == sesion.terapeutaid)
            .first()
        )

    return {
        "id": alerta.id,
        "paciente_id": alerta.paciente_id,
        "tipo": alerta.tipo or "",
        "tipo_label": _tipo_alerta_en_espanol(alerta.tipo),
        "descripcion": alerta.descripcion or "Alerta clínica",
        "fecha": to_ecuador(alerta.fecha),
        "leida": bool(alerta.leida),
        "paciente_nombre": _nombre_completo_persona(paciente) if paciente else None,
        "terapeuta_id": sesion.terapeutaid if sesion else None,
        "terapeuta_nombre": _nombre_completo_persona(terapeuta) if terapeuta else None,
    }


def _validar_alerta_con_acceso(
    db: Session,
    alerta_id: int,
    current_user: Usuario,
) -> Alerta:
    alerta = db.query(Alerta).filter(Alerta.id == alerta_id).first()

    if not alerta:
        raise HTTPException(
            status_code=404,
            detail="Alerta no encontrada",
        )

    paciente = (
        db.query(Paciente)
        .filter(Paciente.id == alerta.paciente_id)
        .first()
    )

    if not paciente:
        raise HTTPException(
            status_code=404,
            detail="Paciente de la alerta no encontrado",
        )

    validar_acceso_paciente_por_rol(paciente, current_user)

    return alerta


@router.get("/", response_model=List[AlertaOut])
def listar_alertas(
    solo_no_leidas: bool = False,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    query = db.query(Alerta).join(
        Paciente,
        Paciente.id == Alerta.paciente_id,
    )

    if current_user.rol == 2:
        query = query.filter(
            Paciente.terapeutaasignadoid == current_user.id
        )

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

    if solo_no_leidas:
        query = query.filter(Alerta.leida == False)

    alertas = query.order_by(Alerta.fecha.desc()).all()

    # Enriquecemos la respuesta para que el front no muestre solo IDs.
    paciente_ids = {a.paciente_id for a in alertas if a.paciente_id}
    pacientes = {}
    if paciente_ids:
        pacientes = {
            p.id: p
            for p in db.query(Paciente).filter(Paciente.id.in_(paciente_ids)).all()
        }

    return [
        _alerta_a_response(
            db=db,
            alerta=alerta,
            paciente=pacientes.get(alerta.paciente_id),
        )
        for alerta in alertas
    ]


@router.put("/{alerta_id}/leer")
def marcar_leida(
    alerta_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    alerta = _validar_alerta_con_acceso(
        db=db,
        alerta_id=alerta_id,
        current_user=current_user,
    )

    alerta.leida = True

    db.commit()

    return {"ok": True}