import uuid
import hashlib
import hmac
import os
import logging
from fastapi import APIRouter, HTTPException, Depends
from supabase import Client

from models.schemas import (
    VerificarDocumentoRequest, VerificarDocumentoResponse,
    SubmitVerificacionRequest, SubmitVerificacionResponse,
    DocumentoExtraido,
)
from services.gemini import verificar_documento
from services.storage import upload_documento, upload_selfie_liveness, upload_selfie_doc
from services.supabase_client import get_supabase

router = APIRouter(prefix="/api/verify", tags=["verificacion"])
logger = logging.getLogger(__name__)


def _doc_hash(numero_doc: str) -> str:
    """
    Hash unidireccional del número de documento.
    Usa HMAC-SHA256 con un salt del entorno — no se puede revertir al número original.
    El salt evita ataques de diccionario (probar todos los DNIs posibles).
    """
    salt = os.environ.get("DOC_HASH_SALT", "cabildoos-default-salt-cambiar-en-prod")
    return hmac.new(
        salt.encode(),
        numero_doc.strip().upper().encode(),
        hashlib.sha256
    ).hexdigest()


@router.post("/documento", response_model=VerificarDocumentoResponse)
async def endpoint_verificar_documento(
    req: VerificarDocumentoRequest,
    supabase: Client = Depends(get_supabase),
):
    """
    Paso 2: verifica duplicados via hash, llama a Gemini Vision y retorna análisis.
    La foto y los datos personales NO se guardan — privacidad por diseño.
    """
    # ── Verificar duplicado ANTES de procesar ─────────────────────────────────
    doc_hash = _doc_hash(req.numero_declarado)
    try:
        existing = supabase.table("verifications") \
            .select("id, status") \
            .eq("doc_hash", doc_hash) \
            .execute()
        if existing.data:
            rec = existing.data[0]
            if rec["status"] in ("aprobado", "pendiente_revision"):
                raise HTTPException(
                    status_code=409,
                    detail="Este documento ya fue usado para verificar una cuenta. Cada documento solo puede usarse una vez."
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error verificando duplicado: {e}")

    # ── Llamar a Gemini Vision ────────────────────────────────────────────────
    extracted = await verificar_documento(
        image_b64=req.image_b64,
        tipo_doc=req.tipo_doc,
        nombre_declarado=req.nombre_declarado,
        apellido_declarado=req.apellido_declarado,
        numero_declarado=req.numero_declarado,
        pais_declarado=req.pais_declarado or "",
    )
    # La imagen y los datos personales se descartan aquí — nunca se persisten

    # ── Guardar hash (anti-duplicado) y resultado en DB ───────────────────────
    match = extracted.nombre_coincide and extracted.numero_coincide and extracted.es_documento_real
    try:
        supabase.table("verifications").upsert({
            "id":       req.verification_id,
            "doc_hash": doc_hash,   # hash unidireccional — no el número real
            "doc_match": match,
            "status":   "en_proceso",
        }).execute()
    except Exception as e:
        logger.warning(f"Error guardando hash: {e}")

    return VerificarDocumentoResponse(
        ok=True,
        match=match,
        extracted=extracted,
        foto_url=None,
    )


@router.post("/submit", response_model=SubmitVerificacionResponse)
async def endpoint_submit_verificacion(
    req: SubmitVerificacionRequest,
    supabase: Client = Depends(get_supabase),
):
    """
    Paso 4 (final): recibe SOLO la foto censurada (selfie sosteniendo el documento
    con los datos del documento pixelados). No se guardan datos personales.
    El admin ve la cara pero nunca el contenido del documento.
    """
    foto_censurada_url = None
    try:
        # selfie_doc_b64 es la imagen ya censurada — cara visible, documento pixelado
        foto_censurada_url = upload_selfie_doc(
            supabase, req.verification_id, req.selfie_doc_b64
        )
    except Exception as e:
        logger.error(f"Error subiendo foto censurada: {e}")
        raise HTTPException(status_code=500, detail="Error subiendo imagen")

    # En DB solo guardamos: ID, timestamp, status y la URL de la foto censurada
    # CERO datos personales (ni nombre, ni DNI, ni email)
    try:
        supabase.table("verifications").upsert({
            "id":              req.verification_id,
            "status":          "pendiente_revision",
            "doc_match":       req.gemini_match,        # true/false del análisis Gemini
            "selfie_doc_url":  foto_censurada_url,      # foto censurada para el admin
        }).execute()
    except Exception as e:
        logger.error(f"Error guardando en DB: {e}")
        raise HTTPException(status_code=500, detail="Error guardando verificación")

    return SubmitVerificacionResponse(
        ok=True,
        verification_id=req.verification_id,
        status="pendiente_revision",
    )


@router.post("/censurar-campos")
async def censurar_campos(body: dict):
    """
    Recibe la foto del selfie sosteniendo el documento.
    Llama a Gemini para detectar las coordenadas de los campos de texto personal
    (nombre, número, fecha, dirección) sin tocar la cara de la persona.
    Devuelve bounding boxes — el cliente aplica la pixelación localmente.
    La imagen NO se guarda.
    """
    import asyncio
    from services.gemini import _call_gemini, _extract_json

    image_b64 = body.get("image_b64", "")
    if not image_b64:
        return {"campos": []}

    prompt = """Esta imagen muestra a una persona sosteniendo un documento de identidad.

Tu tarea: identificar las zonas del documento que contienen datos personales sensibles
(nombre, apellido, número de documento, fecha de nacimiento, dirección, CUIL/CUIT, cualquier código).
NO incluyas la foto/cara que aparece impresa en el documento — solo los campos de texto.
NO incluyas la cara de la persona real que sostiene el documento.

Respondé SOLO con JSON, sin texto adicional:
{
  "campos": [
    {"label": "nombre", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.0},
    {"label": "numero", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": 0.0}
  ]
}

Las coordenadas son fracciones de las dimensiones de la imagen (0.0 = borde izquierdo/superior, 1.0 = borde derecho/inferior).
Si no encontrás el documento, devolvé {"campos": [], "error": "documento no visible"}.
Devolvé todos los campos de texto personal que veas."""

    try:
        raw = await asyncio.to_thread(_call_gemini, prompt, image_b64)
        data = _extract_json(raw)
        return data
    except Exception as e:
        logger.error(f"Error censurar-campos: {e}")
        return {"campos": [], "error": str(e)}


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "cabildoos-api"}


@router.get("/test-gemini")
async def test_gemini():
    """Lista los modelos disponibles con la API key configurada."""
    import os
    import google.generativeai as genai
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "GEMINI_API_KEY no configurada"}
    try:
        genai.configure(api_key=api_key)
        models = [
            m.name for m in genai.list_models()
            if "generateContent" in m.supported_generation_methods
        ]
        return {"ok": True, "modelos_disponibles": models}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)}"}
