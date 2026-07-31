import asyncio
import base64
import io
import json
import re
import logging
from datetime import datetime, timezone
from typing import Optional
import google.generativeai as genai
from PIL import Image

from models.schemas import DocumentoExtraido

logger = logging.getLogger(__name__)

# ── Métricas en memoria (se resetean con cada deploy / reinicio de Render) ────
_GEMINI_MODEL = "gemini-2.0-flash"

# Límites diarios por modelo (free tier). En paid no hay límite RPD, solo TPM.
# Se puede sobreescribir con GEMINI_DAILY_LIMIT env var.
_GEMINI_DEFAULT_LIMITS = {
    "gemini-2.0-flash":      1500,
    "gemini-2.0-flash-lite": 1500,
    "gemini-1.5-flash":      1500,
    "gemini-1.5-pro":        50,
    "gemini-pro":            50,
}

_stats: dict = {
    "requests_today":  0,
    "errors_today":    0,
    "tokens_in":       0,
    "tokens_out":      0,
    "last_request_at": None,
    "last_error":      None,
    "started_at":      datetime.now(timezone.utc).isoformat(),
}

def get_gemini_stats() -> dict:
    import os
    # GEMINI_DAILY_LIMIT env var sobreescribe el default del modelo
    env_limit = os.environ.get("GEMINI_DAILY_LIMIT")
    daily_limit = int(env_limit) if env_limit else _GEMINI_DEFAULT_LIMITS.get(_GEMINI_MODEL, 1500)
    return {
        **_stats,
        "model":       _GEMINI_MODEL,
        "daily_limit": daily_limit,
        "uptime_since": _stats["started_at"],
    }

def _record_call(tokens_in: int = 0, tokens_out: int = 0):
    _stats["requests_today"]  += 1
    _stats["tokens_in"]       += tokens_in
    _stats["tokens_out"]      += tokens_out
    _stats["last_request_at"]  = datetime.now(timezone.utc).isoformat()

def _record_error(msg: str):
    _stats["errors_today"] += 1
    _stats["last_error"]    = msg


def init_gemini(api_key: str):
    genai.configure(api_key=api_key)


# ── Normalización de países ────────────────────────────────────────────────────
_PAIS_VARIANTES: dict[str, list[str]] = {
    "argentina":  ["argentina", "argentino", "argentinas", "republica argentina", "republic of argentina"],
    "venezuela":  ["venezuela", "venezolano", "bolivariana", "republica bolivariana"],
    "colombia":   ["colombia", "colombiano", "colombiana", "republica de colombia"],
    "chile":      ["chile", "chileno", "chilena", "republica de chile"],
    "peru":       ["peru", "peruano", "peruana", "republica del peru"],
    "mexico":     ["mexico", "mexicano", "mexicana", "estados unidos mexicanos"],
    "ecuador":    ["ecuador", "ecuatoriano", "ecuatoriana", "republica del ecuador"],
    "bolivia":    ["bolivia", "boliviano", "boliviana", "estado plurinacional de bolivia"],
    "uruguay":    ["uruguay", "uruguayo", "uruguaya", "republica oriental del uruguay"],
    "paraguay":   ["paraguay", "paraguayo", "paraguaya", "republica del paraguay"],
    "brasil":     ["brasil", "brazil", "brasileiro", "brasileira", "republica federativa do brasil"],
    "espana":     ["espana", "espanol", "espanola", "reino de espana"],
    "usa":        ["united states", "usa", "america", "estados unidos"],
}

def _norm(s: str) -> str:
    """Normaliza un string de país: lowercase, sin acentos, sin espacios extra."""
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower().strip())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")

def _paises_coinciden(pais_declarado: str, pais_emisor: str) -> bool:
    """Compara países tolerando variantes, acentos y prefijos ('República', etc.)."""
    if not pais_declarado or not pais_emisor:
        return False
    pd = _norm(pais_declarado)
    pe = _norm(pais_emisor)
    if pd == pe:
        return True
    # Buscar canónico del país declarado
    canon = next((c for c, vs in _PAIS_VARIANTES.items() if pd == c or pd in vs), None)
    if canon is None:
        # País desconocido — comparación por substring
        return pd in pe or pe in pd
    variantes = _PAIS_VARIANTES[canon]
    return canon in pe or any(v in pe for v in variantes)


def _extract_json(text: str) -> dict:
    """Extrae el primer bloque JSON de la respuesta del modelo."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No se pudo extraer JSON: {text[:300]}")


def _call_gemini(prompt: str, image_b64: str) -> str:
    """Llamada sincrónica a Gemini — se corre en thread separado."""
    model = genai.GenerativeModel(_GEMINI_MODEL)

    image_part = {
        "inline_data": {
            "mime_type": "image/jpeg",
            "data": image_b64,
        }
    }
    response = model.generate_content([prompt, image_part])

    # Registrar tokens si el SDK los expone
    try:
        usage = response.usage_metadata
        _record_call(
            tokens_in  = getattr(usage, "prompt_token_count",     0) or 0,
            tokens_out = getattr(usage, "candidates_token_count", 0) or 0,
        )
    except Exception:
        _record_call()

    return response.text.strip()


def _crop_b64(image_b64: str, bbox: dict) -> str:
    """
    Recorta una región de una imagen base64 y devuelve el recorte como base64.
    bbox tiene x1, y1, x2, y2 como fracciones (0.0–1.0).
    """
    img_bytes = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size

    x1 = int(bbox["x1"] * w)
    y1 = int(bbox["y1"] * h)
    x2 = int(bbox["x2"] * w)
    y2 = int(bbox["y2"] * h)

    # Pequeño margen
    pad = int(min(w, h) * 0.02)
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(w, x2 + pad)
    y2 = min(h, y2 + pad)

    if x2 <= x1 or y2 <= y1:
        return image_b64  # fallback: imagen completa

    face = img.crop((x1, y1, x2, y2))
    buf = io.BytesIO()
    face.save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


async def extraer_cara_documento(image_b64: str) -> Optional[str]:
    """
    Detecta la foto del rostro incrustada en el documento de identidad.
    Retorna la cara recortada como base64, o None si no la detecta.
    """
    prompt = """This is a photo of an identity document (DNI, cedula, passport, ID card, or driver's license).

Find the portrait photo of the person printed on the document — the small embedded face/headshot photo that appears on identity cards.

Return ONLY valid JSON, no extra text:
{
  "face": {
    "x1": 0.05,
    "y1": 0.10,
    "x2": 0.25,
    "y2": 0.55
  }
}

x1,y1 = top-left corner of the face photo on the document (as fractions of total image width/height)
x2,y2 = bottom-right corner

If no embedded face photo is clearly visible, return: {"face": null}"""

    try:
        raw = await asyncio.to_thread(_call_gemini, prompt, image_b64)
        data = _extract_json(raw)
        bbox = data.get("face")
        if not bbox:
            logger.info("extraer_cara_documento: no face detected")
            return None
        cropped = _crop_b64(image_b64, bbox)
        logger.info(f"extraer_cara_documento: cropped face bbox={bbox}")
        return cropped
    except Exception as e:
        _record_error(f"extraer_cara_documento: {e}")
        logger.warning(f"extraer_cara_documento error: {e}")
        return None


async def verificar_documento(
    image_b64: str,
    tipo_doc: str,
    nombre_declarado: str,
    apellido_declarado: str,
    numero_declarado: str,
    pais_declarado: str = "",
    fecha_nac_declarada: str = "",
) -> DocumentoExtraido:
    """
    Llama a Gemini Vision para verificar un documento de identidad.
    Extrae todos los campos y compara con los datos declarados.
    La imagen y los datos extraídos NO se persisten — privacidad por diseño.
    """
    nombres_tipos = {
        "DNI_AR":    "DNI argentino (Documento Nacional de Identidad)",
        "PASAPORTE": "pasaporte internacional",
        "CEDULA_VE": "cédula de identidad venezolana",
        "LICENCIA":  "licencia de conducir argentina",
    }
    desc_tipo = nombres_tipos.get(tipo_doc, tipo_doc)

    prompt = f"""Sos un sistema experto en verificación de documentos de identidad latinoamericanos.

Se te presenta la foto de un {desc_tipo}.

El usuario declaró:
- Nombre: {nombre_declarado}
- Apellido: {apellido_declarado}
- Número de documento: {numero_declarado}
- Nacionalidad / País emisor: {pais_declarado or 'no especificado'}
- Fecha de nacimiento: {fecha_nac_declarada or 'no especificada'}

INSTRUCCIONES IMPORTANTES:
1. Para el DNI argentino: el encabezado dice "REPUBLICA ARGENTINA" o puede incluir "MERCOSUR". MERCOSUR NO es un país — es solo el estándar de formato del documento. El país emisor es SIEMPRE Argentina en ese caso.
2. El campo "Nacionalidad / Nationality" en el documento indica la nacionalidad del titular, NO el país emisor. Ambos suelen coincidir, pero usá el encabezado del documento para determinar el país emisor.
3. Para pais_emisor devolvé SOLO el nombre del país en español, sin prefijos ("Argentina", no "República Argentina").
4. Documentos con hologramas, reflejos o efectos de seguridad siguen siendo documentos reales — no los descartés como falsos por eso.

Analizá la imagen y respondé ÚNICAMENTE con JSON válido, sin texto adicional ni markdown:

{{
  "nombre_completo": "nombre y apellido EXACTO como aparece en el documento, null si no es legible",
  "numero_documento": "número EXACTO como aparece (con o sin puntos), null si no es legible",
  "fecha_nacimiento": "DD/MM/YYYY exacto como aparece, null si no es legible",
  "tipo_documento": "{tipo_doc}",
  "pais_emisor": "SOLO el nombre del país en español (ej: 'Argentina', 'Venezuela', 'Colombia'). Si el encabezado dice REPUBLICA ARGENTINA o ARGENTINA, devolvé 'Argentina'. null si no es legible",
  "es_documento_real": true si es un documento físico real fotografiado (incluyendo documentos con hologramas y efectos de seguridad),
  "nombre_coincide": true si el nombre+apellido del documento coincide con "{nombre_declarado} {apellido_declarado}" (ignorá mayúsculas/minúsculas y acentos),
  "numero_coincide": true si el número del documento coincide con "{numero_declarado}" (ignorá puntos y espacios),
  "fecha_coincide": true si la fecha de nacimiento del documento coincide con "{fecha_nac_declarada}" (ignorá formato, comparar día/mes/año). Si no se declaró fecha, devolver false,
  "pais_coincide": true si pais_emisor coincide con "{pais_declarado}" (Argentina=Argentina, venezolano=Venezuela, etc). Si no se declaró país, devolver true,
  "confianza": número entre 0.0 y 1.0 indicando qué tan legible está el documento,
  "observaciones": "breve descripción de lo que ves, el país detectado, y cualquier problema con la foto"
}}"""

    try:
        # Correr la llamada sincrónica en un thread para no bloquear el event loop
        raw = await asyncio.to_thread(_call_gemini, prompt, image_b64)
        logger.info(f"Gemini raw response: {raw[:500]}")

        data = _extract_json(raw)
        logger.info(f"Gemini parsed: {data}")

        # ── Normalización server-side de pais_coincide ────────────────────────
        # Gemini a veces falla la comparación aunque extrajo el país correcto,
        # o directamente devuelve pais_emisor null si hay hologramas/reflejos.
        # Comparamos nosotros mismos — más confiable que el razonamiento del modelo.
        if pais_declarado and data.get("pais_emisor"):
            # Caso normal: Gemini extrajo pais_emisor → comparamos server-side
            coincide = _paises_coinciden(pais_declarado, data["pais_emisor"])
            if coincide != data.get("pais_coincide"):
                logger.info(
                    f"pais_coincide corregido: Gemini={data.get('pais_coincide')} "
                    f"→ server={coincide} "
                    f"(declarado='{pais_declarado}', emisor='{data['pais_emisor']}')"
                )
            data["pais_coincide"] = coincide
        elif pais_declarado and not data.get("pais_emisor"):
            # Gemini no pudo leer pais_emisor (hologramas, reflejos, foto oscura).
            # Inferimos el país a partir del tipo_doc — DNI_AR siempre es Argentina, etc.
            _DOC_PAIS_MAP: dict[str, str] = {
                "DNI_AR":    "Argentina",
                "LICENCIA":  "Argentina",
                "CEDULA_VE": "Venezuela",
            }
            implied = _DOC_PAIS_MAP.get(tipo_doc, "")
            if implied:
                coincide = _paises_coinciden(pais_declarado, implied)
                logger.info(
                    f"pais_emisor=null → tipo_doc={tipo_doc} → implied='{implied}' → coincide={coincide} "
                    f"(declarado='{pais_declarado}')"
                )
                data["pais_coincide"] = coincide
            else:
                # Tipo de doc desconocido + Gemini no leyó el país → no bloqueamos
                logger.info(f"pais_emisor=null + tipo_doc desconocido '{tipo_doc}' → permissive true")
                data["pais_coincide"] = True
        elif not pais_declarado:
            # Sin país declarado, no bloqueamos
            data["pais_coincide"] = True

        return DocumentoExtraido(**{
            k: data.get(k)
            for k in DocumentoExtraido.model_fields
            if k in data
        })

    except Exception as e:
        _record_error(f"verificar_documento: {type(e).__name__}: {e}")
        logger.error(f"Error Gemini: {type(e).__name__}: {e}")
        return DocumentoExtraido(
            es_documento_real=False,
            confianza=0.0,
            observaciones=f"Error: {type(e).__name__}: {str(e)}"
        )
