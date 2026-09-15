import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..dependencies.db import get_db
from ..models.usuario import Usuario
from ..auth.hashing import verify_password
from ..auth.jwt import create_access_token
from ..auth.dependencies import get_current_user
from ..schemas.token import Token
from ..schemas.usuario import UsuarioOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

logger = logging.getLogger("auth_debug")


@router.post("/login", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    # Antes: Usuario.email == form_data.username (comparación EXACTA).
    # Cualquier mayúscula o espacio de más que el teclado del celular
    # inserte sin que se note en pantalla (autocapitalización, autocompletado)
    # hacía fallar el login aunque el texto se viera idéntico.
    # Ahora se normaliza (trim + lower) a ambos lados de la comparación.
    email_normalizado = form_data.username.strip().lower()
    user = (
        db.query(Usuario)
        .filter(func.lower(func.trim(Usuario.email)) == email_normalizado)
        .first()
    )

    # --- Diagnóstico temporal (quitar una vez identificada la causa) ---
    # Nunca se registra la contraseña en texto plano, solo su longitud,
    # para poder distinguir si el fallo es por email no encontrado o por
    # una contraseña que no coincide byte a byte con la esperada
    # (ej. un espacio de más insertado por el teclado del celular).
    if user is None:
        logger.warning(
            "LOGIN FALLIDO - email no encontrado. Recibido=%r (len=%d)",
            form_data.username,
            len(form_data.username),
        )
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")

    if not verify_password(form_data.password, user.passwordhash):
        logger.warning(
            "LOGIN FALLIDO - password no coincide para email=%s. "
            "Longitud recibida=%d (se esperaba una contraseña sin espacios extra)",
            user.email,
            len(form_data.password),
        )
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
    # --- Fin diagnóstico temporal ---

    access_token = create_access_token(data={"sub": str(user.id)})
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me")
def get_me(
    current_user: Usuario = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Devuelve los datos del usuario y su foto_url firmada en un solo request.
    El cliente ya no necesita llamar a /usuarios/me/foto-url por separado.
    """
    from ..services.supabase_storage import crear_url_firmada_foto_usuario
    from storage3.exceptions import StorageApiError

    foto_url: str | None = None

    if current_user.fotourl:
        try:
            foto_url = crear_url_firmada_foto_usuario(
                current_user.fotourl,
                segundos=3600,
            )
        except (StorageApiError, Exception):
            foto_url = None

    return {
        "id": current_user.id,
        "nombres": current_user.nombres,
        "apellidos": current_user.apellidos,
        "email": current_user.email,
        "rol": current_user.rol,
        "fotourl": current_user.fotourl,
        "foto_url_firmada": foto_url,        # ← nuevo campo
        "consultorioid": current_user.consultorioid,
        "activo": current_user.activo,
        "fecharegistro": current_user.fecharegistro,
    }