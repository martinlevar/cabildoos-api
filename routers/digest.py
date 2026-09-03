import asyncio
import logging
import os
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from supabase import Client

from services.digest import (
    enviar_digest,
    obtener_datos_ayer,
    generar_resumen_gemini,
    construir_email_html,
)
from services.supabase_client import get_supabase

router = APIRouter(prefix="/api/digest", tags=["digest"])
logger = logging.getLogger(__name__)


def _check_secret(x_digest_secret: str = Header(default="")):
    secret = os.environ.get("DIGEST_SECRET", "")
    if not secret or x_digest_secret != secret:
        raise HTTPException(status_code=401, detail="Unauthorized")


@router.post("/send")
async def send_digest(
    _: None = Depends(_check_secret),
    supabase: Client = Depends(get_supabase),
):
    """
    Dispara el email digest diario a todos los usuarios verificados.
    Requiere header X-Digest-Secret con el valor de la env var DIGEST_SECRET.
    """
    try:
        result = await enviar_digest(supabase)
        logger.info(f"Digest enviado: {result}")
        return result
    except Exception as e:
        logger.error(f"Error enviando digest: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/preview", response_class=HTMLResponse)
async def preview_digest(
    _: None = Depends(_check_secret),
    supabase: Client = Depends(get_supabase),
):
    """
    Preview del email de ayer en HTML, sin enviarlo.
    Útil para revisar el contenido antes del envío.
    """
    datos = await asyncio.to_thread(obtener_datos_ayer, supabase)
    resumen = await generar_resumen_gemini(datos)
    html = construir_email_html(datos, resumen)
    return HTMLResponse(content=html)
