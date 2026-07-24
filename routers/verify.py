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


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "cabildoos-api"}
