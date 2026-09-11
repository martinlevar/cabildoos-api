"""
services/gemini.py — Cliente Gemini para verificación, vocería y digest.

Funciones exportadas:
  - verificar_documento()     → verifica cédula/pasaporte con Gemini Vision
  - extraer_cara_documento()  → extrae la foto del documento (cropped)
  - _call_gemini()            → llamada genérica Gemini Vision (prompt + imagen)
  - _extract_json()           → parsea JSON de respuesta Gemini
  - _gemini_client            → instancia del cliente (usada por digest/voceria/redes)
  - _GEMINI_MODEL             → nombre del modelo activo
  - _record_call()            → contabiliza llamadas
  - _record_error()           → contabiliza errores
  - get_gemini_stats()        → stats para /api/admin/status/gemini
"""

import asyncio
import base64
import json
import logging
import os
import re

logger = logging.getLogger(__name__)

# ── Configuración ───────────────────────────────────────────────────────────

_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

_gemini_client = None


def _init_client():
    global _gemini_client
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY no configurada — funciones Gemini deshabilitadas")
        return
    try:
        from google import genai
        _gemini_client = genai.Client(api_key=api_key)
        logger.info("Gemini client inicializado (model=%s)", _GEMINI_MODEL)
    except Exception as exc:
        logger.error("Error inicializando Gemini client: %s", exc)


_init_client()


# ── Contadores en memoria ───────────────────────────────────────────────────

_stats: dict = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}


def _record_call(tokens_in: int = 0, tokens_out: int = 0) -> None:
    _stats["calls"] += 1
    _stats["tokens_in"] += tokens_in
    _stats["tokens_out"] += tokens_out


def _record_error(msg: str = "") -> None:
    _stats["errors"] += 1
    logger.warning("Gemini error registrado: %s", msg)


def get_gemini_stats() -> dict:
    return {
        "status": "enabled" if _gemini_client else "disabled",
        "model": _GEMINI_MODEL,
        **_stats,
    }


# ── Llamada genérica Vision ─────────────────────────────────────────────────

def _call_gemini(prompt: str, image_b64: str) -> str:
    """
    Llama a Gemini con texto + imagen. Retorna el texto de la respuesta.
    Función SINCRÓNICA — usar con asyncio.to_thread desde endpoints async.
    """
    client = _gemini_client
    if client is None:
        raise RuntimeError("Gemini client no inicializado (falta GEMINI_API_KEY)")

    from google.genai import types

    image_bytes = base64.b64decode(image_b64)

    # Detectar mime type por magic bytes
    if image_bytes[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"

    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime),
        types.Part.from_text(text=prompt),
    ]

    response = client.models.generate_content(
        model=_GEMINI_MODEL,
        contents=contents,
    )

    try:
        usage = response.usage_metadata
        _record_call(
            tokens_in=getattr(usage, "prompt_token_count", 0) or 0,
            tokens_out=getattr(usage, "candidates_token_count", 0) or 0,
        )
    except Exception:
        _record_call()

    return response.text.strip()


def _extract_json(text: str) -> dict:
    """Extrae el primer bloque JSON válido de una respuesta de Gemini."""
    # Intentar parsear directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Buscar bloque ```json ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Buscar primer { ... } en el texto
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return {}


# ── Verificación de documento ───────────────────────────────────────────────

def _verificar_documento_sync(
    image_b64: str,
    tipo_doc: str,
    nombre_declarado: str,
    apellido_declarado: str,
    numero_declarado: str,
    pais_declarado: str = "",
    fecha_nac_declarada: str = "",
) -> "DocumentoExtraido":
    from models.schemas import DocumentoExtraido

    prompt = f"""You are a document verification system. Analyze this identity document image carefully.

DECLARED DATA TO VERIFY:
- Full name: {nombre_declarado} {apellido_declarado}
- Document number: {numero_declarado}
- Document type: {tipo_doc}
- Country: {pais_declarado or "any"}
- Date of birth: {fecha_nac_declarada or "not provided"}

INSTRUCTIONS:
1. Determine if this is a real, physical identity document (not a photocopy of a screen, not a digital screenshot, not a fake)
2. Extract the visible data from the document
3. Compare extracted data with the declared data above

Return ONLY valid JSON, no extra text:
{{
  "es_documento_real": true or false,
  "nombre_completo": "full name as it appears on document or null",
  "numero_documento": "document number as it appears or null",
  "fecha_nacimiento": "date of birth as it appears or null",
  "tipo_documento": "type of document detected",
  "pais_emisor": "issuing country or null",
  "nombre_coincide": true or false,
  "numero_coincide": true or false,
  "fecha_coincide": true or false,
  "pais_coincide": true or false,
  "confianza": 0.0 to 1.0,
  "observaciones": "brief notes about the verification or any issues found"
}}

For name matching: consider accents, abbreviations, and order variations as matching if clearly the same person.
For date matching: if date of birth was not provided, set fecha_coincide to true.
For country matching: if country was not provided, set pais_coincide to true."""

    try:
        raw = _call_gemini(prompt, image_b64)
        data = _extract_json(raw)

        return DocumentoExtraido(
            nombre_completo=data.get("nombre_completo"),
            numero_documento=data.get("numero_documento"),
            fecha_nacimiento=data.get("fecha_nacimiento"),
            tipo_documento=data.get("tipo_documento"),
            pais_emisor=data.get("pais_emisor"),
            es_documento_real=bool(data.get("es_documento_real", False)),
            nombre_coincide=bool(data.get("nombre_coincide", False)),
            numero_coincide=bool(data.get("numero_coincide", False)),
            fecha_coincide=bool(data.get("fecha_coincide", not bool(fecha_nac_declarada))),
            pais_coincide=bool(data.get("pais_coincide", not bool(pais_declarado))),
            confianza=float(data.get("confianza", 0.0)),
            observaciones=data.get("observaciones"),
        )
    except Exception as e:
        _record_error(f"verificar_documento: {e}")
        logger.error("Error verificando documento: %s", e)
        return DocumentoExtraido(
            es_documento_real=False,
            observaciones=f"Error: {type(e).__name__}: {e}",
        )


async def verificar_documento(
    image_b64: str,
    tipo_doc: str,
    nombre_declarado: str,
    apellido_declarado: str,
    numero_declarado: str,
    pais_declarado: str = "",
    fecha_nac_declarada: str = "",
):
    return await asyncio.to_thread(
        _verificar_documento_sync,
        image_b64, tipo_doc, nombre_declarado, apellido_declarado,
        numero_declarado, pais_declarado, fecha_nac_declarada,
    )


# ── Extracción de cara del documento ───────────────────────────────────────

def _extraer_cara_sync(image_b64: str):
    """
    Pide a Gemini que identifique si hay una foto de cara en el documento.
    Retorna None si no puede extraerla — el proceso continúa sin la foto.
    """
    prompt = """This image shows an identity document. 
    
Does this document contain a photo/portrait of a person's face?

Return ONLY valid JSON:
{
  "tiene_foto": true or false,
  "observaciones": "brief note"
}"""

    try:
        raw = _call_gemini(prompt, image_b64)
        data = _extract_json(raw)
        if data.get("tiene_foto"):
            # Retornamos el b64 original — el cropping se hace client-side
            return image_b64
        return None
    except Exception as e:
        _record_error(f"extraer_cara: {e}")
        logger.warning("No se pudo extraer cara del documento: %s", e)
        return None


async def extraer_cara_documento(image_b64: str):
    return await asyncio.to_thread(_extraer_cara_sync, image_b64)
