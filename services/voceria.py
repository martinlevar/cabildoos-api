"""
Vocería del Cabildo — servicio para anuncios institucionales.

Genera anuncios via Gemini y construye el HTML del email de difusión.
"""
import asyncio
import html as _html
import logging

logger = logging.getLogger(__name__)


def _esc(s) -> str:
    return _html.escape(str(s or ""), quote=False).encode("ascii", "xmlcharrefreplace").decode("ascii")


# ── Gemini ─────────────────────────────────────────────────────────────────

def _generar_anuncio_sync(prompt_admin: str) -> dict:
    """Llama a Gemini con el prompt del admin y devuelve {titulo, texto}."""
    from services.gemini import _gemini_client, _GEMINI_MODEL, _record_call, _record_error

    client = _gemini_client
    if client is None:
        return {
            "titulo": "Comunicado del Cabildo",
            "texto": "El servicio de generación no está disponible en este momento.",
        }

    prompt = f"""Sos el portavoz del Cabildo de Venezuela. Redactá un comunicado institucional EN ESPAÑOL basado en las siguientes instrucciones del equipo:

INSTRUCCIONES:
{prompt_admin}

FORMATO DE RESPUESTA (solo esto, sin markdown ni comentarios extra):
TÍTULO: <título breve del comunicado, máximo 10 palabras>
TEXTO: <cuerpo del comunicado, 2 a 4 párrafos, tono formal pero cercano, sin markdown ni símbolos especiales, separar párrafos con saltos de línea>"""

    try:
        response = client.models.generate_content(
            model=_GEMINI_MODEL,
            contents=[prompt],
        )
        try:
            usage = response.usage_metadata
            _record_call(
                tokens_in=getattr(usage, "prompt_token_count", 0) or 0,
                tokens_out=getattr(usage, "candidates_token_count", 0) or 0,
            )
        except Exception:
            _record_call()

        raw = response.text.strip()
        titulo = "Comunicado del Cabildo"
        texto = raw

        # Parse TÍTULO / TEXTO format
        lines = raw.splitlines()
        titulo_lines, texto_lines, in_texto = [], [], False
        for line in lines:
            if line.upper().startswith("TÍTULO:") or line.upper().startswith("TITULO:"):
                titulo_lines.append(line.split(":", 1)[1].strip())
            elif line.upper().startswith("TEXTO:"):
                in_texto = True
                rest = line.split(":", 1)[1].strip()
                if rest:
                    texto_lines.append(rest)
            elif in_texto:
                texto_lines.append(line)

        if titulo_lines:
            titulo = " ".join(titulo_lines).strip()
        if texto_lines:
            texto = "\n".join(texto_lines).strip()

        return {"titulo": titulo, "texto": texto}

    except Exception as e:
        _record_error(f"voceria_gemini: {e}")
        logger.error(f"Gemini vocería error: {e}")
        return {
            "titulo": "Comunicado del Cabildo",
            "texto": "El comunicado no pudo generarse en este momento. Por favor intente nuevamente.",
        }


async def generar_anuncio_gemini(prompt_admin: str) -> dict:
    return await asyncio.to_thread(_generar_anuncio_sync, prompt_admin)


# ── Email HTML ──────────────────────────────────────────────────────────────

def construir_email_anuncio(titulo: str, texto: str) -> str:
    """Construye el HTML del email de vocería con la misma identidad visual del digest."""
    from services.digest import ARC_URL, LOGO_DATA

    ARC_BG = ARC_URL

    titulo_esc = _esc(titulo)

    parrafos_html = "".join(
        f'<p style="font-size:15px;color:#374151;line-height:1.8;margin:0 0 16px 0;">{_esc(par.strip())}</p>'
        for par in texto.split("\n") if par.strip()
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo_esc} — Cabildo de Venezuela</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
  <tr><td align="center">
    <table role="presentation" width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;background:#ffffff;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.10);overflow:hidden;">

      <!-- HEADER -->
      <tr><td style="background:#0a0f1e;background-image:url('{ARC_BG}');background-size:cover;background-position:center top;border-radius:16px 16px 0 0;padding:48px 48px 36px;text-align:center;border-bottom:1px solid #1e3a5f;">
        <img src="{LOGO_DATA}" width="120" height="120" alt="Cabildo de Venezuela" style="display:block;margin:0 auto 20px;border-radius:50%;object-fit:cover;">
        <div style="font-size:11px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:rgba(255,255,255,0.55);margin-bottom:8px;">Vocería del Cabildo</div>
        <div style="font-size:22px;font-weight:800;color:#ffffff;line-height:1.3;max-width:440px;margin:0 auto;">{titulo_esc}</div>
      </td></tr>

      <!-- BODY -->
      <tr><td style="padding:40px 48px 32px;">
        {parrafos_html}
      </td></tr>

      <!-- DIVIDER -->
      <tr><td style="padding:0 48px;"><div style="height:1px;background:#e5e7eb;"></div></td></tr>

      <!-- FOOTER -->
      <tr><td style="padding:28px 48px 36px;text-align:center;">
        <p style="font-size:12px;color:#9ca3af;margin:0 0 8px 0;">Cabildo de Venezuela — Democracia participativa en acción</p>
        <p style="font-size:11px;color:#d1d5db;margin:0;">
          <a href="https://cabildodevenezuela.com" style="color:#C9A84C;text-decoration:none;">cabildodevenezuela.com</a>
        </p>
      </td></tr>

    </table>
  </td></tr>
</table>
</body>
</html>"""
