from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.orm import Session
from typing import List, Optional
from storage3.exceptions import StorageApiError

from ..dependencies.db import get_db
from ..models.usuario import Usuario
from ..auth.dependencies import get_current_user, get_current_jefe
from ..core.security import get_password_hash
from ..schemas.usuario import UsuarioCreate, UsuarioOut, UsuarioUpdate
from ..services.supabase_storage import (
    subir_foto_usuario,
    eliminar_foto_usuario,
    crear_url_firmada_foto_usuario,
)

router = APIRouter(prefix="/api/usuarios", tags=["usuarios"])


def validar_usuario_requiere_consultorio(
    rol: Optional[int],
    consultorioid: Optional[int],
) -> None:
    """
    Rol 1 = Secretario
    Rol 2 = Terapeuta

    Ambos deben tener consultorio asignado.
    """
    if rol in (1, 2) and consultorioid is None:
        raise HTTPException(
            status_code=400,
            detail="Secretarios y terapeutas deben tener consultorio asignado.",
        )


@router.get("/", response_model=List[UsuarioOut])
def listar_usuarios(
    rol: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    """
    Lista usuarios según el rol del usuario autenticado:

    - Secretario rol=1:
      Solo ve terapeutas activos de su mismo consultorio.

    - Jefe rol=3:
      Ve todos los usuarios activos.
      Puede filtrar por rol.
    """

    if current_user.rol == 1:
        if current_user.consultorioid is None:
            raise HTTPException(
                status_code=403,
                detail="El secretario no tiene consultorio asignado.",
            )

        query = db.query(Usuario).filter(
            Usuario.rol == 2,
            Usuario.activo == True,
            Usuario.consultorioid == current_user.consultorioid,
        )

    elif current_user.rol == 3:
        query = db.query(Usuario).filter(Usuario.activo == True)

        if rol is not None:
            query = query.filter(Usuario.rol == rol)

    else:
        raise HTTPException(
            status_code=403,
            detail="No autorizado",
        )

    return query.order_by(
        Usuario.apellidos.asc(),
        Usuario.nombres.asc(),
    ).all()


@router.post("/", response_model=UsuarioOut, status_code=status.HTTP_201_CREATED)
def crear_usuario(
    usuario: UsuarioCreate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    existing = db.query(Usuario).filter(Usuario.email == usuario.email).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail="El email ya está registrado.",
        )

    validar_usuario_requiere_consultorio(
        usuario.rol,
        usuario.consultorioid,
    )

    nuevo = Usuario(
        nombres=usuario.nombres,
        apellidos=usuario.apellidos,
        email=usuario.email,
        passwordhash=get_password_hash(usuario.password),
        rol=usuario.rol,
        fotourl=usuario.fotourl,
        consultorioid=usuario.consultorioid,
        activo=True,
    )

    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)

    return nuevo


@router.put("/{usuario_id}", response_model=UsuarioOut)
def actualizar_usuario(
    usuario_id: int,
    usuario: UsuarioUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    db_usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()

    if not db_usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado.",
        )

    if usuario.email is not None:
        email_existente = (
            db.query(Usuario)
            .filter(
                Usuario.email == usuario.email,
                Usuario.id != usuario_id,
            )
            .first()
        )

        if email_existente:
            raise HTTPException(
                status_code=400,
                detail="Ya existe otro usuario con ese email.",
            )

        db_usuario.email = usuario.email

    if usuario.nombres is not None:
        db_usuario.nombres = usuario.nombres

    if usuario.apellidos is not None:
        db_usuario.apellidos = usuario.apellidos

    if usuario.rol is not None:
        db_usuario.rol = usuario.rol

    if "fotourl" in usuario.model_fields_set:
        foto_anterior = db_usuario.fotourl

        db_usuario.fotourl = usuario.fotourl

        if usuario.fotourl is None and foto_anterior:
            try:
                eliminar_foto_usuario(foto_anterior)
            except Exception as e:
                print("No se pudo eliminar la foto anterior:", str(e))

    if usuario.consultorioid is not None:
        db_usuario.consultorioid = usuario.consultorioid

    validar_usuario_requiere_consultorio(
        db_usuario.rol,
        db_usuario.consultorioid,
    )

    if usuario.password:
        db_usuario.passwordhash = get_password_hash(usuario.password)

    db.commit()
    db.refresh(db_usuario)

    return db_usuario

@router.put("/me/perfil", response_model=UsuarioOut)
def actualizar_mi_perfil(
    usuario: UsuarioUpdate,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    db_usuario = db.query(Usuario).filter(Usuario.id == current_user.id).first()

    if not db_usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado.",
        )

    if usuario.email is not None:
        email_existente = (
            db.query(Usuario)
            .filter(
                Usuario.email == usuario.email,
                Usuario.id != current_user.id,
            )
            .first()
        )

        if email_existente:
            raise HTTPException(
                status_code=400,
                detail="Ya existe otro usuario con ese email.",
            )

        db_usuario.email = usuario.email

    if usuario.nombres is not None:
        db_usuario.nombres = usuario.nombres

    if usuario.apellidos is not None:
        db_usuario.apellidos = usuario.apellidos

    if "fotourl" in usuario.model_fields_set:
        foto_anterior = db_usuario.fotourl
        db_usuario.fotourl = usuario.fotourl

        if usuario.fotourl is None and foto_anterior:
            try:
                eliminar_foto_usuario(foto_anterior)
            except Exception as e:
                print("No se pudo eliminar la foto anterior:", str(e))

    # Importante:
    if usuario.password:
        db_usuario.passwordhash = get_password_hash(usuario.password)
    # Eso queda solo para el endpoint del jefe.

    db.commit()
    db.refresh(db_usuario)

    return db_usuario


@router.delete("/{usuario_id}")
def eliminar_usuario(
    usuario_id: int,
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_jefe),
):
    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()

    if not usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado.",
        )

    usuario.activo = False

    db.commit()

    return {"ok": True}


@router.post("/me/foto", status_code=status.HTTP_200_OK)
async def actualizar_mi_foto(
    foto: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    usuario = db.query(Usuario).filter(Usuario.id == current_user.id).first()

    if not usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado.",
        )

    foto_anterior = usuario.fotourl

    nueva_foto_path = await subir_foto_usuario(
        archivo=foto,
        usuario_id=usuario.id,
    )

    usuario.fotourl = nueva_foto_path

    db.commit()
    db.refresh(usuario)

    if foto_anterior:
        try:
            eliminar_foto_usuario(foto_anterior)
        except Exception as e:
            print("No se pudo eliminar la foto anterior:", str(e))

    return {
        "message": "Foto actualizada correctamente.",
        "fotourl": usuario.fotourl,
    }

@router.get("/me/foto-url")
def obtener_mi_foto_url(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    usuario = db.query(Usuario).filter(Usuario.id == current_user.id).first()

    if not usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado.",
        )

    if not usuario.fotourl:
        raise HTTPException(
            status_code=404,
            detail="El usuario no tiene foto registrada.",
        )

    try:
        url = crear_url_firmada_foto_usuario(
            usuario.fotourl,
            segundos=3600,
        )

    except StorageApiError:
        usuario.fotourl = None
        db.commit()

        raise HTTPException(
            status_code=404,
            detail="La foto registrada ya no existe. Vuelve a subir una foto.",
        )

    except Exception as e:
        print("Error generando URL firmada de foto:", str(e))

        raise HTTPException(
            status_code=500,
            detail="No se pudo generar la URL de la foto.",
        )

    return {
        "url": url,
        "expira_en_segundos": 3600,
    }


@router.delete("/me/foto")
def eliminar_mi_foto(
    db: Session = Depends(get_db),
    current_user: Usuario = Depends(get_current_user),
):
    usuario = db.query(Usuario).filter(Usuario.id == current_user.id).first()

    if not usuario:
        raise HTTPException(
            status_code=404,
            detail="Usuario no encontrado.",
        )

    foto_anterior = usuario.fotourl

    if not foto_anterior:
        return {
            "message": "El usuario no tiene foto registrada.",
            "fotourl": None,
        }

    # 1. Primero limpiar BD
    usuario.fotourl = None
    db.commit()
    db.refresh(usuario)

    # 2. Después intentar borrar archivo en Supabase
    try:
        eliminada = eliminar_foto_usuario(foto_anterior)
        print("Foto eliminada de Supabase:", eliminada)
    except Exception as e:
        print("No se pudo eliminar la foto en Supabase:", str(e))

    return {
        "message": "Foto eliminada correctamente.",
        "fotourl": usuario.fotourl,
    }