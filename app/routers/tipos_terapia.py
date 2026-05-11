from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..auth.dependencies import get_current_secretary, get_current_user
from ..dependencies.db import get_db
from ..models.tipo_terapia import TipoTerapia
from ..schemas.tipo_terapia import (
    TipoTerapiaCreate,
    TipoTerapiaOut,
    TipoTerapiaUpdate,
)

router = APIRouter(
    prefix="/api/tipos-terapia",
    tags=["tipos-terapia"],
)


@router.get("/", response_model=List[TipoTerapiaOut])
def listar_tipos_terapia(
    solo_activos: bool = False,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    query = db.query(TipoTerapia)

    if solo_activos:
        query = query.filter(TipoTerapia.activo == True)

    return query.order_by(TipoTerapia.nombre.asc()).all()


@router.get("/{tipo_id}", response_model=TipoTerapiaOut)
def obtener_tipo_terapia(
    tipo_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    tipo = db.query(TipoTerapia).filter(TipoTerapia.id == tipo_id).first()

    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo de terapia no encontrado")

    return tipo


@router.post("/", response_model=TipoTerapiaOut, status_code=status.HTTP_201_CREATED)
def crear_tipo_terapia(
    data: TipoTerapiaCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_secretary),
):
    nombre = data.nombre.strip()

    existe = (
        db.query(TipoTerapia)
        .filter(TipoTerapia.nombre.ilike(nombre))
        .first()
    )

    if existe:
        raise HTTPException(
            status_code=400,
            detail="Ya existe un tipo de terapia con ese nombre.",
        )

    nuevo = TipoTerapia(
        nombre=nombre,
        descripcion=data.descripcion,
        precio_sesion=data.precio_sesion,
        activo=data.activo,
    )

    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)

    return nuevo


@router.put("/{tipo_id}", response_model=TipoTerapiaOut)
def actualizar_tipo_terapia(
    tipo_id: int,
    data: TipoTerapiaUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_secretary),
):
    tipo = db.query(TipoTerapia).filter(TipoTerapia.id == tipo_id).first()

    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo de terapia no encontrado")

    datos = data.model_dump(exclude_unset=True)

    if "nombre" in datos and datos["nombre"] is not None:
        nombre = datos["nombre"].strip()

        existe = (
            db.query(TipoTerapia)
            .filter(
                TipoTerapia.nombre.ilike(nombre),
                TipoTerapia.id != tipo_id,
            )
            .first()
        )

        if existe:
            raise HTTPException(
                status_code=400,
                detail="Ya existe otro tipo de terapia con ese nombre.",
            )

        tipo.nombre = nombre

    if "descripcion" in datos:
        tipo.descripcion = datos["descripcion"]

    if "precio_sesion" in datos and datos["precio_sesion"] is not None:
        tipo.precio_sesion = datos["precio_sesion"]

    if "activo" in datos and datos["activo"] is not None:
        tipo.activo = datos["activo"]

    db.commit()
    db.refresh(tipo)

    return tipo


@router.patch("/{tipo_id}/estado", response_model=TipoTerapiaOut)
def cambiar_estado_tipo_terapia(
    tipo_id: int,
    activo: bool,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_secretary),
):
    tipo = db.query(TipoTerapia).filter(TipoTerapia.id == tipo_id).first()

    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo de terapia no encontrado")

    tipo.activo = activo

    db.commit()
    db.refresh(tipo)

    return tipo


@router.delete("/{tipo_id}")
def desactivar_tipo_terapia(
    tipo_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_secretary),
):
    tipo = db.query(TipoTerapia).filter(TipoTerapia.id == tipo_id).first()

    if not tipo:
        raise HTTPException(status_code=404, detail="Tipo de terapia no encontrado")

    tipo.activo = False

    db.commit()

    return {"ok": True, "message": "Tipo de terapia desactivado correctamente"}