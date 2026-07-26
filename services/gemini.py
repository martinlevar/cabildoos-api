import asyncio
import base64
import io
import json
import re
import logging
from typing import Optional
import google.generativeai as genai
from PIL import Image

from models.schemas import DocumentoExtraido

logger = logging.getLogger(__name__)


def init_gemini(api_key: str):
    genai.configure(api_key=api_key)


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
    model = genai.GenerativeModel("gemini-3.5-flash")

    # Pasar imagen directamente como inline_data (más confiable que PIL)
    image_part = {
        "inline_data": {
            "mime_type": "image/jpeg",
            "data": image_b64,
        }
    }
    response = model.generate_content([prompt, image_part])
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

    prompt = f"""Sos un sistema experto en verificación de documentos de identidad.

Se te presenta la foto de un {desc_tipo}.

El usuario declaró:
- Nombre: {nombre_declarado}
- Apellido: {apellido_declarado}
- Número de documento: {numero_declarado}
- Nacionalidad / País emisor: {pais_declarado or 'no especificado'}
- Fecha de nacimiento: {fecha_nac_declarada or 'no especificada'}

Analizá la imagen y respondé ÚNICAMENTE con JSON válido, sin texto adicional ni markdown:

{{
  "nombre_completo": "nombre y apellido EXACTO como aparece en el documento, null si no es legible",
  "numero_documento": "número EXACTO como aparece (con o sin puntos), null si no es legible",
  "fecha_nacimiento": "DD/MM/YYYY exacto como aparece, null si no es legible",
  "tipo_documento": "{tipo_doc}",
  "pais_emisor": "país que emitió el documento según lo que aparece en él, en español y en su forma completa (ej: Venezuela, Argentina, Chile, Colombia). null si no es legible",
  "es_documento_real": true si es un documento físico real fotografiado (no una pantalla, no una fotocopia, no una imagen digital),
  "nombre_coincide": true si el nombre+apellido del documento coincide con "{nombre_declarado} {apellido_declarado}" (ignorá mayúsculas/minúsculas y acentos),
  "numero_coincide": true si el número del documento coincide con "{numero_declarado}" (ignorá puntos y espacios),
  "fecha_coincide": true si la fecha de nacimiento del documento coincide con "{fecha_nac_declarada}" (ignorá formato, comparar día/mes/año). Si no se declaró fecha, devolver false,
  "pais_coincide": true si el país emisor del documento coincide con "{pais_declarado}" (aceptá variantes: venezolano=Venezuela, argentino=Argentina, etc). Si el usuario no declaró país, devolver false,
  "confianza": número entre 0.0 y 1.0 indicando qué tan legible está el documento,
  "observaciones": "breve descripción de lo que ves, el país detectado, y cualquier problema con la foto"
}}"""

    try:
        # Correr la llamada sincrónica en un thread para no bloquear el event loop
        raw = await asyncio.to_thread(_call_gemini, prompt, image_b64)
        logger.info(f"Gemini raw response: {raw[:500]}")

        data = _extract_json(raw)
        logger.info(f"Gemini parsed: {data}")

        return DocumentoExtraido(**{
            k: data.get(k)
            for k in DocumentoExtraido.model_fields
            if k in data
        })

    except Exception as e:
        logger.error(f"Error Gemini: {type(e).__name__}: {e}")
        return DocumentoExtraido(
            es_documento_real=False,
            confianza=0.0,
            observaciones=f"Error: {type(e).__name__}: {str(e)}"
        )
