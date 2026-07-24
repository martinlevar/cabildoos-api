import base64
import uuid
import logging
from supabase import Client

logger = logging.getLogger(__name__)

BUCKET = "verifications"


def _ensure_bucket(supabase: Client):
    """Crea el bucket si no existe (idempotente)."""
    try:
        supabase.storage.create_bucket(
            BUCKET,
            options={"public": False, "file_size_limit": 10 * 1024 * 1024}  # 10MB
        )
    except Exception:
        pass  # Ya existe


def upload_b64(supabase: Client, b64: str, path: str) -> str:
    """
    Sube una imagen base64 a Supabase Storage.
    Retorna la URL pública firmada (1 año).
    """
    _ensure_bucket(supabase)
    data = base64.b64decode(b64)

    supabase.storage.from_(BUCKET).upload(
        path,
        data,
        file_options={"content-type": "image/jpeg", "upsert": "true"}
    )

    # URL firmada válida por 1 año (para que admin pueda revisar)
    signed = supabase.storage.from_(BUCKET).create_signed_url(
        path, expires_in=365 * 24 * 3600
    )
    return signed["signedURL"]


def upload_documento(supabase: Client, verification_id: str, b64: str) -> str:
    path = f"{verification_id}/documento.jpg"
    return upload_b64(supabase, b64, path)


def upload_selfie_liveness(supabase: Client, verification_id: str, b64: str) -> str:
    path = f"{verification_id}/selfie_liveness.jpg"
    return upload_b64(supabase, b64, path)


def upload_selfie_doc(supabase: Client, verification_id: str, b64: str) -> str:
    path = f"{verification_id}/selfie_documento.jpg"
    return upload_b64(supabase, b64, path)
