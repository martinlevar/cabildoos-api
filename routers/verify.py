import uuid
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


@router.post("/documento", response_model=VerificarDocumentoResponse)
async def endpoint_verificar_documento(
    req: VerificarDocumentoRequest,
):
    """
    Paso 2: recibe foto del documento, llama a Gemini Vision y retorna el análisis.
    La foto y los datos extraídos NO se guardan — privacidad por diseño.
    Solo el resultado (match: bool) llega al frontend.
    """
    extracted = await verificar_documento(
        image_b64=req.image_b64,
        tipo_doc=req.tipo_doc,
        nombre_declarado=req.nombre_declarado,
        apellido_declarado=req.apellido_declarado,
        numero_declarado=req.numero_declarado,
    )
    # La imagen se descarta aquí — nunca se persiste en el servidor
    match = extracted.nombre_coincide and extracted.numero_coincide and extracted.es_documento_real

    return VerificarDocumentoResponse(
        ok=True,
        match=match,
        extracted=extracted,
        foto_url=None,   # nunca se guarda
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
