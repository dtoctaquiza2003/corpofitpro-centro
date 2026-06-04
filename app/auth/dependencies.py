"""
app/auth/dependencies.py — versión corregida
============================================
Corrección aplicada vs versión anterior del ZIP:
  - NO usa Usuario.__new__(Usuario) — eso rompe SQLAlchemy (_sa_instance_state faltante)
  - Usa SimpleNamespace: objeto liviano sin estado ORM, funciona perfectamente
    porque los endpoints solo leen current_user.id / .rol / .consultorioid / etc.
  - En cache HIT: 0 queries a la DB
  - En cache MISS (primera vez o tras TTL 60s): 1 query a la DB, igual que antes
"""

from types import SimpleNamespace
from typing import Optional

from cachetools import TTLCache
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from ..auth.jwt import decode_access_token
from ..dependencies.db import get_db
from ..models.usuario import Usuario

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

# TTL 60s: buen balance entre reducir queries y detectar cambios de usuario.
# maxsize=200: suficiente para ~200 sesiones activas simultáneas.
_user_cache: TTLCache = TTLCache(maxsize=200, ttl=60)


def _usuario_a_cache(user: Usuario) -> dict:
    """Guarda solo primitivos — nunca el objeto ORM ni la sesión de SQLAlchemy."""
    return {
        "id": user.id,
        "rol": user.rol,
        "activo": user.activo,
        "consultorioid": user.consultorioid,
        "nombres": user.nombres,
        "apellidos": user.apellidos,
        "email": user.email,
        "fotourl": user.fotourl,
    }


def _cache_a_usuario_sin_db(cached: dict) -> SimpleNamespace:
    """
    Construye un objeto liviano desde memoria. CERO queries a la DB.

    SimpleNamespace es suficiente porque todos los endpoints acceden a
    current_user solo por atributos simples:
        current_user.id, .rol, .activo, .consultorioid, .nombres, etc.

    NO se usa Usuario.__new__(Usuario) porque SQLAlchemy requiere
    _sa_instance_state en el objeto y sin él pueden ocurrir errores
    al intentar acceder a relaciones o al hacer db.add(current_user).
    """
    return SimpleNamespace(**cached)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> SimpleNamespace:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # ── Cache HIT: 0 queries ───────────────────────────────────────────────
    if token in _user_cache:
        return _cache_a_usuario_sin_db(_user_cache[token])

    # ── Cache MISS: verificar en DB (primera vez o tras TTL) ──────────────
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise credentials_exception

    user = (
        db.query(Usuario)
        .filter(Usuario.id == user_id, Usuario.activo == True)
        .first()
    )

    if user is None:
        raise credentials_exception

    _user_cache[token] = _usuario_a_cache(user)
    return user


def invalidate_user_cache(token: str) -> None:
    """Llamar en logout para forzar re-autenticación inmediata."""
    _user_cache.pop(token, None)


# ---------------------------------------------------------------------------
# Dependencias de rol — sin cambios de lógica
# ---------------------------------------------------------------------------

def get_current_terapeuta(
    current_user: SimpleNamespace = Depends(get_current_user),
) -> SimpleNamespace:
    if current_user.rol != 2:
        raise HTTPException(status_code=403, detail="Solo terapeutas pueden acceder")
    return current_user


def get_current_jefe(
    current_user: SimpleNamespace = Depends(get_current_user),
) -> SimpleNamespace:
    if current_user.rol != 3:
        raise HTTPException(status_code=403, detail="Solo jefes pueden acceder")
    return current_user


def get_current_secretary(
    current_user: SimpleNamespace = Depends(get_current_user),
) -> SimpleNamespace:
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