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
    supabase: Client = Depends(get_supabase),
):
    """
    Paso 2: recibe foto del documento, llama a Gemini Vision,
    sube la foto a Storage y retorna el análisis.
    """
    # 1. Llamar a Gemini Vision
    extracted = await verificar_documento(
        image_b64=req.image_b64,
        tipo_doc=req.tipo_doc,
        nombre_declarado=req.nombre_declarado,
        apellido_declarado=req.apellido_declarado,
        numero_declarado=req.numero_declarado,
    )

    # 2. Subir foto a Supabase Storage
    foto_url = None
    try:
        foto_url = upload_documento(supabase, req.verification_id, req.image_b64)
    except Exception as e:
        logger.warning(f"Error subiendo foto documento: {e}")

    # 3. Guardar resultado parcial en DB (upsert)
    try:
        supabase.table("verifications").upsert({
            "id": req.verification_id,
            "tipo_doc": req.tipo_doc,
            "nombre": req.nombre_declarado,
            "apellido": req.apellido_declarado,
            "numero_doc": req.numero_declarado,
            "doc_foto_url": foto_url,
            "doc_extracted": extracted.model_dump(),
            "doc_match": extracted.nombre_coincide and extracted.numero_coincide,
            "doc_confianza": extracted.confianza,
            "status": "en_proceso",
        }).execute()
    except Exception as e:
        logger.warning(f"Error guardando en DB: {e}")

    match = extracted.nombre_coincide and extracted.numero_coincide and extracted.es_documento_real

    return VerificarDocumentoResponse(
        ok=True,
        match=match,
        extracted=extracted,
        foto_url=foto_url,
    )


@router.post("/submit", response_model=SubmitVerificacionResponse)
async def endpoint_submit_verificacion(
    req: SubmitVerificacionRequest,
    supabase: Client = Depends(get_supabase),
):
    """
    Paso 4 (final): sube las fotos de selfie liveness y selfie+documento,
    guarda todo en DB y marca la verificación como pendiente de revisión.
    """
    selfie_liveness_url = None
    selfie_doc_url = None

    # Subir fotos
    try:
        selfie_liveness_url = upload_selfie_liveness(
            supabase, req.verification_id, req.selfie_liveness_b64
        )
    except Exception as e:
        logger.warning(f"Error subiendo selfie liveness: {e}")

    try:
        selfie_doc_url = upload_selfie_doc(
            supabase, req.verification_id, req.selfie_doc_b64
        )
    except Exception as e:
        logger.warning(f"Error subiendo selfie doc: {e}")

    # Auto-aprobar si confianza alta + match + documento real
    auto_approve = (
        req.doc_extracted is not None
        and req.doc_extracted.es_documento_real
        and req.doc_extracted.nombre_coincide
        and req.doc_extracted.numero_coincide
        and req.doc_extracted.confianza >= 0.85
    )
    status = "auto_aprobado" if auto_approve else "pendiente_revision"

    # Guardar en DB
    try:
        supabase.table("verifications").upsert({
            "id": req.verification_id,
            "nombre": req.datos_declarados.nombre,
            "apellido": req.datos_declarados.apellido,
            "numero_doc": req.datos_declarados.numero_doc,
            "tipo_doc": req.datos_declarados.tipo_doc,
            "fecha_nac": req.datos_declarados.fecha_nac,
            "email": req.datos_declarados.email,
            "telefono": req.datos_declarados.telefono,
            "selfie_liveness_url": selfie_liveness_url,
            "selfie_doc_url": selfie_doc_url,
            "liveness_instruccion": req.liveness_instruccion,
            "status": status,
        }).execute()
    except Exception as e:
        logger.error(f"Error guardando verificacion final: {e}")
        raise HTTPException(status_code=500, detail="Error guardando verificación")

    return SubmitVerificacionResponse(
        ok=True,
        verification_id=req.verification_id,
        status=status,
    )


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "cabildoos-api"}
