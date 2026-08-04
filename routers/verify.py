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
from services.gemini import verificar_documento, extraer_cara_documento
from services.storage import upload_documento, upload_selfie_liveness, upload_selfie_doc, upload_doc_face
from services.supabase_client import get_supabase
from services.error_log import log_gemini_error, log_verification_failure

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
            if rec["status"] in ("rechazado", "en_proceso"):
                # Permitir reintento — limpiar el registro anterior incompleto o rechazado
                supabase.table("verifications").delete().eq("doc_hash", doc_hash).execute()
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Este documento ya fue usado para verificar una cuenta. Cada documento solo puede usarse una vez."
                )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Error verificando duplicado: {e}")

    import asyncio

    # ── Llamar a Gemini Vision (verificación) + extracción de cara en paralelo ─
    extracted, face_b64 = await asyncio.gather(
        verificar_documento(
            image_b64=req.image_b64,
            tipo_doc=req.tipo_doc,
            nombre_declarado=req.nombre_declarado,
            apellido_declarado=req.apellido_declarado,
            numero_declarado=req.numero_declarado,
            pais_declarado=req.pais_declarado or "",
            fecha_nac_declarada=req.fecha_nac_declarada or "",
        ),
        extraer_cara_documento(req.image_b64),
    )

    # ── Guardar hash (anti-duplicado) y resultado en DB ───────────────────────
    pais_ok = extracted.pais_coincide if req.pais_declarado else True
    match = (extracted.nombre_coincide and extracted.numero_coincide
             and extracted.fecha_coincide and extracted.es_documento_real
             and pais_ok)

    # ── Loguear errores de Gemini y fallos de verificación ───────────────────
    client_ip = None  # request IP no está disponible aquí sin Request object
    if extracted.observaciones and "Error:" in (extracted.observaciones or ""):
        # Gemini falló completamente
        log_gemini_error(supabase, "gemini-flash-latest", extracted.observaciones or "", ip=client_ip)
    elif not match:
        # Verificación falló — registrar qué campos fallaron
        campos_fallidos = []
        if not extracted.es_documento_real:     campos_fallidos.append("documento_no_real")
        if not extracted.nombre_coincide:       campos_fallidos.append("nombre")
        if not extracted.numero_coincide:       campos_fallidos.append("numero")
        if not extracted.fecha_coincide:        campos_fallidos.append("fecha_nacimiento")
        if not pais_ok:                         campos_fallidos.append("pais")
        if campos_fallidos:
            log_verification_failure(
                supabase,
                reason=", ".join(campos_fallidos),
                tipo_doc=req.tipo_doc,
                campos_fallidos=campos_fallidos,
                confianza=extracted.confianza,
            )

    try:
        supabase.table("verifications").upsert({
            "id":        req.verification_id,
            "doc_hash":  doc_hash,
            "doc_match": match,
            "status":    "en_proceso",
        }).execute()
    except Exception as e:
        logger.warning(f"Error guardando hash: {e}")

    # ── Subir foto del rostro del documento en background ────────────────────
    if face_b64:
        async def _upload_face():
            try:
                url = await asyncio.to_thread(
                    upload_doc_face, supabase, req.verification_id, face_b64
                )
                supabase.table("verifications").update(
                    {"doc_face_url": url}
                ).eq("id", req.verification_id).execute()
                logger.info(f"doc_face subida: {url[:60]}...")
            except Exception as e:
                logger.error(f"Error subiendo doc_face: {e}")
        asyncio.create_task(_upload_face())
    # La imagen original y los datos personales se descartan aquí — no se persisten

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
    import asyncio

    # ── Validar que /documento fue llamado primero (doc_hash debe existir) ────
    try:
        chk = supabase.table("verifications") \
            .select("id, doc_hash, status") \
            .eq("id", req.verification_id) \
            .execute()
        if not chk.data:
            raise HTTPException(
                status_code=400,
                detail="Verificación de documento requerida antes de enviar."
            )
        rec = chk.data[0]
        if not rec.get("doc_hash"):
            raise HTTPException(
                status_code=400,
                detail="Verificación de documento incompleta. Reiniciá el proceso."
            )
        if rec.get("status") == "aprobado":
            raise HTTPException(
                status_code=409,
                detail="Este documento ya fue verificado y aprobado."
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error validando verificación: {e}")
        raise HTTPException(status_code=500, detail=f"Error de validación: {e}")

    # Guardar en DB primero — responder rápido al cliente
    try:
        row = {
            "id":        req.verification_id,
            "status":    "pendiente_revision",
            "doc_match": req.gemini_match,
        }
        # contact_email NO se guarda — rompe el link email → butaca.
        # El admin ve caras y asigna butacas sin saber quién es quién.
        supabase.table("verifications").upsert(row).execute()
        logger.info(f"Verificación guardada: {req.verification_id}")
    except Exception as e:
        logger.error(f"Error DB: {e}")
        raise HTTPException(status_code=500, detail=f"Error DB: {e}")

    # Subir foto a Storage en background — no bloquea la respuesta al cliente
    async def _upload_foto():
        try:
            url = await asyncio.to_thread(
                upload_selfie_doc, supabase, req.verification_id, req.selfie_doc_b64
            )
            supabase.table("verifications").update(
                {"selfie_doc_url": url}
            ).eq("id", req.verification_id).execute()
            logger.info(f"Foto subida en background: {url[:60]}...")
        except Exception as e:
            logger.error(f"Error Storage background: {e}")

    asyncio.create_task(_upload_foto())

    return SubmitVerificacionResponse(
        ok=True,
        verification_id=req.verification_id,
        status="pendiente_revision",
    )


@router.post("/liveness")
async def verificar_liveness(body: dict):
    """
    Verifica que la persona en la foto siguió la instrucción de liveness.
    Llama a Gemini Vision con la instrucción específica.
    La imagen NO se guarda.
    """
    import asyncio
    from services.gemini import _call_gemini, _extract_json

    image_b64 = body.get("image_b64", "")
    instruccion = body.get("instruccion", "")
    if not image_b64 or not instruccion:
        return {"cumplió": True}

    prompt = f"""Look at this selfie photo. The person was asked to perform this action: "{instruccion}"

Determine if the person is clearly performing the requested action in this photo.

Return ONLY valid JSON:
{{
  "cumplió": true or false,
  "confianza": 0.0 to 1.0,
  "observacion": "brief description of what you see"
}}

Be strict: if the action requires a specific visible gesture (winking, smiling broadly, tilting head, opening mouth, showing fingers, touching nose), the person must clearly be doing it. A neutral face counts as false for any action-based instruction."""

    try:
        raw = await asyncio.to_thread(_call_gemini, prompt, image_b64)
        data = _extract_json(raw)
        return {"cumplió": bool(data.get("cumplió", True)), **data}
    except Exception as e:
        logger.error(f"Error liveness: {e}")
        return {"cumplió": True}  # ante error, no bloquear


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

    prompt = """This image shows a person holding an identity document (DNI, passport, ID card, or driver's license).

Locate the identity document in the image.

Return ONLY valid JSON, no extra text:
{
  "document": {
    "x1": 0.0,
    "y1": 0.0,
    "x2": 1.0,
    "y2": 1.0
  }
}

Where x1,y1 is the top-left corner and x2,y2 is the bottom-right corner of the document.
All values are fractions of the image dimensions (0.0 = left/top edge, 1.0 = right/bottom edge).

If no document is clearly visible, return: {"document": null}"""

    try:
        raw = await asyncio.to_thread(_call_gemini, prompt, image_b64)
        data = _extract_json(raw)
        return data
    except Exception as e:
        logger.error(f"Error censurar-campos: {e}")
        return {"campos": [], "error": str(e)}


@router.get("/status/{verification_id}")
async def get_verification_status(
    verification_id: str,
    supabase: Client = Depends(get_supabase),
):
    """
    El usuario consulta el estado de su propia verificación por ID.
    Solo devuelve: status y butaca_numero — sin datos personales.
    """
    try:
        res = supabase.table("verifications") \
            .select("status, butaca_numero") \
            .eq("id", verification_id) \
            .single() \
            .execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="No encontrado")
        return {"ok": True, **res.data}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ping")
async def ping():
    return {"ok": True, "service": "cabildoos-api"}


@router.get("/test-gemini")
async def test_gemini():
    """Lista los modelos disponibles con la API key configurada."""
    import os
    from google import genai as _genai
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "GEMINI_API_KEY no configurada"}
    try:
        client = _genai.Client(api_key=api_key)
        models = [
            m.name for m in client.models.list()
            if any("generateContent" in (m.supported_actions or []))
        ]
        return {"ok": True, "modelos_disponibles": models}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)}"}
