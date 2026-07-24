import base64
import json
import re
import logging
from typing import Optional
import google.generativeai as genai
from PIL import Image
import io

from models.schemas import DocumentoExtraido

logger = logging.getLogger(__name__)


def init_gemini(api_key: str):
    genai.configure(api_key=api_key)


def _b64_to_pil(b64: str) -> Image.Image:
    """Convierte base64 (sin prefijo data:) a imagen PIL."""
    data = base64.b64decode(b64)
    return Image.open(io.BytesIO(data))


def _extract_json(text: str) -> dict:
    """Extrae el primer bloque JSON de la respuesta del modelo."""
    # Intentar parsear directo
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Buscar bloque JSON entre ```
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    # Buscar primer { ... }
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    raise ValueError(f"No se pudo extraer JSON de la respuesta: {text[:200]}")


async def verificar_documento(
    image_b64: str,
    tipo_doc: str,
    nombre_declarado: str,
    apellido_declarado: str,
    numero_declarado: str,
) -> DocumentoExtraido:
    """
    Llama a Gemini Vision para verificar un documento de identidad.
    Extrae campos y compara con los datos declarados por el usuario.
    """
    try:
        img = _b64_to_pil(image_b64)
    except Exception as e:
        logger.error(f"Error decodificando imagen: {e}")
        return DocumentoExtraido(observaciones=f"Imagen inválida: {e}")

    nombres_tipos = {
        "DNI_AR": "DNI argentino (Documento Nacional de Identidad)",
        "PASAPORTE": "pasaporte internacional",
        "CEDULA_VE": "cédula de identidad venezolana",
        "LICENCIA": "licencia de conducir argentina",
    }
    desc_tipo = nombres_tipos.get(tipo_doc, tipo_doc)

    prompt = f"""Sos un sistema experto en verificación de documentos de identidad para una plataforma de democracia participativa venezolana.

Se te presenta la foto de un {desc_tipo}.

El usuario declaró:
- Nombre: {nombre_declarado}
- Apellido: {apellido_declarado}
- Número de documento: {numero_declarado}

Tu tarea:
1. Verificar que la imagen es realmente un documento de identidad físico (no una pantalla fotografiada, no una fotocopia de pantalla, no una imagen digital).
2. Extraer los datos visibles del documento.
3. Comparar los datos extraídos con lo que declaró el usuario.

Respondé ÚNICAMENTE con JSON válido, sin texto adicional, sin markdown:

{{
  "nombre_completo": "nombre y apellido como aparece en el documento, o null si no es legible",
  "numero_documento": "número exacto como aparece, o null si no es legible",
  "fecha_nacimiento": "DD/MM/YYYY o null",
  "tipo_documento": "{tipo_doc}",
  "es_documento_real": true o false (¿es un documento físico real, no una pantalla/imagen?),
  "nombre_coincide": true o false (¿el nombre+apellido del documento coincide con '{nombre_declarado} {apellido_declarado}'?),
  "numero_coincide": true o false (¿el número coincide con '{numero_declarado}'?),
  "confianza": número entre 0.0 y 1.0 (qué tan legible y confiable es el documento),
  "observaciones": "cualquier nota relevante sobre el documento o la foto"
}}"""

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content([prompt, img])
        raw = response.text.strip()
        logger.info(f"Gemini response: {raw[:300]}")

        data = _extract_json(raw)
        return DocumentoExtraido(**{
            k: data.get(k)
            for k in DocumentoExtraido.model_fields
            if k in data
        })

    except Exception as e:
        logger.error(f"Error llamando a Gemini: {e}")
        return DocumentoExtraido(
            es_documento_real=False,
            confianza=0.0,
            observaciones=f"Error de análisis: {str(e)}"
        )
