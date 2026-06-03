from dataclasses import dataclass
from typing import Optional

from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from ..auth.jwt import decode_access_token
from ..dependencies.db import get_db
from ..models.usuario import Usuario

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

# Cacheamos solo los datos primitivos, NO el objeto ORM
@dataclass
class UsuarioCacheado:
    id: int
    rol: int
    activo: bool
    consultorioid: Optional[int]
    nombres: str
    apellidos: str
    email: str
    fotourl: Optional[str]

_user_cache: TTLCache = TTLCache(maxsize=200, ttl=60)


def _usuario_a_cache(user: Usuario) -> UsuarioCacheado:
    return UsuarioCacheado(
        id=user.id,
        rol=user.rol,
        activo=user.activo,
        consultorioid=user.consultorioid,
        nombres=user.nombres,
        apellidos=user.apellidos,
        email=user.email,
        fotourl=user.fotourl,
    )


def _cache_a_usuario(db: Session, cached: UsuarioCacheado) -> Optional[Usuario]:
    """
    Recarga el objeto ORM desde la sesión actual usando el ID cacheado.
    """
    return (
        db.query(Usuario)
        .filter(
            Usuario.id == cached.id,
            Usuario.activo == True,
        )
        .first()
    )


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> Usuario:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if token in _user_cache:
        cached = _user_cache[token]
        user = _cache_a_usuario(db, cached)
        if not user:
            del _user_cache[token]
            raise credentials_exception
        return user

    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise credentials_exception

    user = db.query(Usuario).filter(
        Usuario.id == user_id,
        Usuario.activo == True,
    ).first()

    if user is None:
        raise credentials_exception

    _user_cache[token] = _usuario_a_cache(user)
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