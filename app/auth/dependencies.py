from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from jose import JWTError
from cachetools import TTLCache
from ..dependencies.db import get_db
from ..models.usuario import Usuario
from .jwt import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

# Caché en memoria: máximo 200 tokens, cada uno válido 60 segundos.
# Con 60s un usuario que hace 10 requests seguidas solo toca la DB 1 vez.
_user_cache: TTLCache = TTLCache(maxsize=200, ttl=60)

async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Usuario:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # Si el token ya está en caché, no tocamos la DB
    if token in _user_cache:
        return _user_cache[token]

    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    user_id: int = payload.get("sub")
    if user_id is None:
        raise credentials_exception

    user = db.query(Usuario).filter(
        Usuario.id == user_id,
        Usuario.activo == True,
    ).first()

    if user is None:
        raise credentials_exception

    _user_cache[token] = user
    return user


def get_current_terapeuta(current_user: Usuario = Depends(get_current_user)) -> Usuario:
    if current_user.rol != 2:
        raise HTTPException(status_code=403, detail="Solo terapeutas pueden acceder")
    return current_user


def get_current_jefe(current_user: Usuario = Depends(get_current_user)) -> Usuario:
    if current_user.rol != 3:
        raise HTTPException(status_code=403, detail="Solo jefes pueden acceder")
    return current_user


def get_current_secretary(current_user: Usuario = Depends(get_current_user)) -> Usuario:
    if current_user.rol not in (1, 3):
        raise HTTPException(
            status_code=403,
            detail="Permiso denegado. Se requiere secretario o jefe.",
        )
    if current_user.rol == 1 and current_user.consultorioid is None:
        raise HTTPException(
            status_code=403,
            detail="El secretario no tiene consultorio asignado.",
        )
    return current_user