# Inicializador del módulo auth
from .jwt import create_access_token, decode_access_token
from .hashing import verify_password, get_password_hash
from .dependencies import (
    get_current_user,
    get_current_terapeuta,
    get_current_jefe,
    get_current_secretary,
    oauth2_scheme
)