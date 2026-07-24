import asyncio
import base64
import json
import re
import logging
from typing import Optional
import google.generativeai as genai

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


async def verificar_documento(
    image_b64: str,
    tipo_doc: str,
    nombre_declarado: str,
    apellido_declarado: str,
    numero_declarado: str,
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

Analizá la imagen y respondé ÚNICAMENTE con JSON válido, sin texto adicional ni markdown:

{{
  "nombre_completo": "nombre y apellido EXACTO como aparece en el documento, null si no es legible",
  "numero_documento": "número EXACTO como aparece (con o sin puntos), null si no es legible",
  "fecha_nacimiento": "DD/MM/YYYY exacto como aparece, null si no es legible",
  "tipo_documento": "{tipo_doc}",
  "es_documento_real": true si es un documento físico real fotografiado, false si es una pantalla o imagen digital,
  "nombre_coincide": true si el nombre+apellido del documento coincide con "{nombre_declarado} {apellido_declarado}" (ignorá mayúsculas/minúsculas y acentos),
  "numero_coincide": true si el número del documento coincide con "{numero_declarado}" (ignorá puntos y espacios),
  "confianza": número entre 0.0 y 1.0 indicando qué tan legible está el documento,
  "observaciones": "breve descripción de lo que ves y cualquier problema con la foto"
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
