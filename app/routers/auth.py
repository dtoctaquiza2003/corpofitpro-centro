from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from ..dependencies.db import get_db
from ..models.usuario import Usuario
from ..auth.hashing import verify_password
from ..auth.jwt import create_access_token
from ..auth.dependencies import get_current_user
from ..schemas.token import Token
from ..schemas.usuario import UsuarioOut

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    user = db.query(Usuario).filter(Usuario.email == form_data.username).first()
    if not user or not verify_password(form_data.password, user.passwordhash):
        raise HTTPException(status_code=401, detail="Credenciales incorrectas")
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