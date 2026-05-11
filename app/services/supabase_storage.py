from datetime import datetime
from uuid import uuid4
from urllib.parse import urlparse, unquote

from fastapi import UploadFile, HTTPException
from supabase import create_client

from app.core.config import settings


if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError(
        "Faltan SUPABASE_URL o SUPABASE_SERVICE_ROLE_KEY en el .env"
    )

supabase = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_SERVICE_ROLE_KEY,
)


def _normalizar_storage_path(path: str | None, bucket: str) -> str | None:
    """
    Convierte cualquier valor guardado en BD a una ruta válida dentro del bucket.

    Ejemplos:
    - usuario_1/foto.jpg                         -> usuario_1/foto.jpg
    - /usuario_1/foto.jpg                        -> usuario_1/foto.jpg
    - usuarios-fotos/usuario_1/foto.jpg          -> usuario_1/foto.jpg
    - https://.../storage/v1/object/sign/...     -> usuario_1/foto.jpg
    - https://.../storage/v1/object/public/...   -> usuario_1/foto.jpg
    """
    if not path:
        return None

    clean_path = unquote(str(path).strip())

    if not clean_path:
        return None

    if clean_path.startswith("http://") or clean_path.startswith("https://"):
        parsed = urlparse(clean_path)
        clean_path = parsed.path

        marker_public = f"/storage/v1/object/public/{bucket}/"
        marker_sign = f"/storage/v1/object/sign/{bucket}/"

        if marker_public in clean_path:
            clean_path = clean_path.split(marker_public, 1)[1]
        elif marker_sign in clean_path:
            clean_path = clean_path.split(marker_sign, 1)[1]
        else:
            clean_path = clean_path.split("/")[-1]

    clean_path = clean_path.lstrip("/")

    bucket_prefix = f"{bucket}/"
    if clean_path.startswith(bucket_prefix):
        clean_path = clean_path[len(bucket_prefix):]

    return clean_path or None


async def subir_comprobante_pago(
    archivo: UploadFile,
    paciente_id: int,
) -> str:
    tipos_permitidos = [
        "image/jpeg",
        "image/png",
        "image/webp",
    ]

    if archivo.content_type not in tipos_permitidos:
        raise HTTPException(
            status_code=400,
            detail="El comprobante debe ser una imagen JPG, PNG o WEBP.",
        )

    contenido = await archivo.read()

    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="El comprobante no debe superar los 5 MB.",
        )

    extension = archivo.filename.split(".")[-1].lower()

    if extension not in ["jpg", "jpeg", "png", "webp"]:
        extension = "jpg"

    fecha = datetime.now().strftime("%Y%m%d_%H%M%S")

    path = (
        f"paciente_{paciente_id}/"
        f"comprobante_{fecha}_{uuid4().hex}.{extension}"
    )

    supabase.storage.from_(settings.SUPABASE_BUCKET_COMPROBANTES).upload(
        path=path,
        file=contenido,
        file_options={
            "content-type": archivo.content_type,
            "upsert": "false",
        },
    )

    return path


def crear_url_firmada_comprobante(path: str, segundos: int = 3600) -> str:
    clean_path = _normalizar_storage_path(
        path,
        settings.SUPABASE_BUCKET_COMPROBANTES,
    )

    if not clean_path:
        raise HTTPException(
            status_code=404,
            detail="No existe ruta del comprobante.",
        )

    response = (
        supabase.storage
        .from_(settings.SUPABASE_BUCKET_COMPROBANTES)
        .create_signed_url(clean_path, segundos)
    )

    signed_url = response.get("signedURL") or response.get("signed_url")

    if not signed_url:
        raise HTTPException(
            status_code=500,
            detail="No se pudo generar la URL del comprobante.",
        )

    return signed_url


async def subir_foto_usuario(
    archivo: UploadFile,
    usuario_id: int,
) -> str:
    tipos_permitidos = [
        "image/jpeg",
        "image/png",
        "image/webp",
    ]

    if archivo.content_type not in tipos_permitidos:
        raise HTTPException(
            status_code=400,
            detail="La foto debe ser una imagen JPG, PNG o WEBP.",
        )

    contenido = await archivo.read()

    if len(contenido) > 5 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="La foto no debe superar los 5 MB.",
        )

    extension = archivo.filename.split(".")[-1].lower()

    if extension not in ["jpg", "jpeg", "png", "webp"]:
        extension = "jpg"

    fecha = datetime.now().strftime("%Y%m%d_%H%M%S")

    path = (
        f"usuario_{usuario_id}/"
        f"foto_{fecha}_{uuid4().hex}.{extension}"
    )

    supabase.storage.from_(settings.SUPABASE_BUCKET_USUARIOS).upload(
        path=path,
        file=contenido,
        file_options={
            "content-type": archivo.content_type,
            "upsert": "false",
        },
    )

    return path


def eliminar_foto_usuario(path: str | None) -> bool:
    clean_path = _normalizar_storage_path(
        path,
        settings.SUPABASE_BUCKET_USUARIOS,
    )

    if not clean_path:
        return False

    response = (
        supabase.storage
        .from_(settings.SUPABASE_BUCKET_USUARIOS)
        .remove([clean_path])
    )

    print("Respuesta eliminar foto Supabase:", response)

    return True


def crear_url_firmada_foto_usuario(path: str, segundos: int = 3600) -> str:
    clean_path = _normalizar_storage_path(
        path,
        settings.SUPABASE_BUCKET_USUARIOS,
    )

    if not clean_path:
        raise HTTPException(
            status_code=404,
            detail="El usuario no tiene foto registrada.",
        )

    response = (
        supabase.storage
        .from_(settings.SUPABASE_BUCKET_USUARIOS)
        .create_signed_url(clean_path, segundos)
    )

    signed_url = response.get("signedURL") or response.get("signed_url")

    if not signed_url:
        raise HTTPException(
            status_code=500,
            detail="No se pudo generar la URL de la foto.",
        )

    return signed_url