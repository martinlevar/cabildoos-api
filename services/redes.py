"""
Redes Sociales — servicio para adaptar comunicados a formatos de redes.

Toma el contenido de un comunicado (Secretaría/Vocería) y genera:
- Texto para X/Twitter (máx 280 chars)
- Caption para IG Post (caption + hashtags)
- Texto para IG Story (muy breve, impactante)
- Flyer HTML visual para cada plataforma
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# ── Colores y branding Cabildo ──────────────────────────────────────────────
_NAVY    = "#0a0f1e"
_BLUE    = "#1e3a5f"
_GOLD    = "#C9A84C"
_WHITE   = "#ffffff"
_LIGHT   = "#f1f5f9"


# ── Gemini ──────────────────────────────────────────────────────────────────

def _generar_redes_sync(titulo: str, texto: str) -> dict:
    """Llama a Gemini y devuelve {x, ig_post, ig_story}."""
    from services.gemini import _gemini_client, _GEMINI_MODEL, _record_call, _record_error

    client = _gemini_client
    if client is None:
        return _fallback(titulo, texto)

    prompt = f"""Sos el community manager del Cabildo de Venezuela. Adaptá el siguiente comunicado institucional para tres plataformas de redes sociales. Respondé SOLO en el formato indicado, sin markdown, sin comentarios extra.

COMUNICADO:
TÍTULO: {titulo}
TEXTO: {texto}

FORMATO DE RESPUESTA (respetá exactamente estas etiquetas):
X: <texto para X/Twitter, máximo 280 caracteres, directo, sin hashtags, tono institucional pero cercano>
IGPOST: <caption para Instagram Post, 2-3 párrafos cortos, tono cálido, terminá con 3-5 hashtags relevantes en español separados por espacios>
IGSTORY: <texto muy breve para IG Story, máximo 2 líneas, impactante, puede usar emojis>"""

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
        result = {"x": "", "ig_post": "", "ig_story": ""}

        lines = raw.splitlines()
        current_key = None
        buffers = {"x": [], "ig_post": [], "ig_story": []}

        for line in lines:
            upper = line.upper()
            if upper.startswith("X:"):
                current_key = "x"
                rest = line.split(":", 1)[1].strip()
                if rest:
                    buffers["x"].append(rest)
            elif upper.startswith("IGPOST:"):
                current_key = "ig_post"
                rest = line.split(":", 1)[1].strip()
                if rest:
                    buffers["ig_post"].append(rest)
            elif upper.startswith("IGSTORY:"):
                current_key = "ig_story"
                rest = line.split(":", 1)[1].strip()
                if rest:
                    buffers["ig_story"].append(rest)
            elif current_key:
                buffers[current_key].append(line)

        result["x"]        = "\n".join(buffers["x"]).strip()
        result["ig_post"]  = "\n".join(buffers["ig_post"]).strip()
        result["ig_story"] = "\n".join(buffers["ig_story"]).strip()

        # Truncar X a 280 chars si Gemini se pasó
        if len(result["x"]) > 280:
            result["x"] = result["x"][:277] + "…"

        return result

    except Exception as e:
        _record_error(f"redes_gemini: {e}")
        logger.error(f"Gemini redes error: {e}")
        return _fallback(titulo, texto)


def _fallback(titulo: str, texto: str) -> dict:
    resumen = texto[:200].strip()
    if len(texto) > 200:
        resumen += "…"
    return {
        "x":        f"{titulo}. {resumen}"[:280],
        "ig_post":  f"{titulo}\n\n{resumen}\n\n#CabildoVenezuela #Democracia",
        "ig_story": f"📢 {titulo}",
    }


async def generar_contenido_redes(titulo: str, texto: str) -> dict:
    return await asyncio.to_thread(_generar_redes_sync, titulo, texto)


# ── Flyers HTML ─────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    import html as _html
    return _html.escape(str(s or ""), quote=False).encode("ascii", "xmlcharrefreplace").decode("ascii")


def _logo_data() -> str:
    try:
        from services.digest import LOGO_DATA
        return LOGO_DATA
    except Exception:
        return ""


def generar_flyer_x(titulo: str, texto_x: str) -> str:
    """Flyer estilo tweet card para X/Twitter."""
    t_esc = _esc(titulo)
    x_esc = _esc(texto_x)
    logo  = _logo_data()
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flyer X — {t_esc}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    width:1200px; height:628px; overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:{_NAVY};
    display:flex; align-items:center; justify-content:center;
  }}
  .card {{
    width:100%; height:100%;
    background:linear-gradient(135deg,{_NAVY} 0%,{_BLUE} 60%,#0d2340 100%);
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    padding:60px 80px; position:relative; overflow:hidden;
  }}
  .bg-circle {{
    position:absolute; border-radius:50%;
    background:radial-gradient(circle,{_GOLD}18 0%,transparent 70%);
  }}
  .bg-circle.c1 {{ width:500px;height:500px;top:-150px;right:-100px; }}
  .bg-circle.c2 {{ width:300px;height:300px;bottom:-80px;left:-60px; }}
  .logo {{
    width:72px; height:72px; border-radius:50%; object-fit:cover;
    border:2px solid {_GOLD}60; margin-bottom:24px;
  }}
  .eyebrow {{
    font-size:12px; font-weight:700; letter-spacing:0.2em;
    text-transform:uppercase; color:{_GOLD}; margin-bottom:16px;
  }}
  .titulo {{
    font-size:38px; font-weight:800; color:{_WHITE};
    line-height:1.2; text-align:center; max-width:800px; margin-bottom:32px;
  }}
  .divider {{ width:60px; height:3px; background:{_GOLD}; border-radius:2px; margin-bottom:32px; }}
  .texto {{
    font-size:20px; color:rgba(255,255,255,0.80); line-height:1.6;
    text-align:center; max-width:900px;
  }}
  .footer {{
    position:absolute; bottom:24px; left:0; right:0;
    display:flex; align-items:center; justify-content:center; gap:8px;
  }}
  .footer span {{ font-size:13px; color:rgba(255,255,255,0.35); letter-spacing:0.05em; }}
  .x-badge {{
    position:absolute; top:28px; right:36px;
    background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.15);
    border-radius:20px; padding:6px 16px;
    font-size:13px; color:rgba(255,255,255,0.55); font-weight:600;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="bg-circle c1"></div>
  <div class="bg-circle c2"></div>
  <div class="x-badge">X / Twitter</div>
  {'<img class="logo" src="' + logo + '" alt="Cabildo">' if logo else ''}
  <div class="eyebrow">Cabildo de Venezuela</div>
  <div class="titulo">{t_esc}</div>
  <div class="divider"></div>
  <div class="texto">{x_esc}</div>
  <div class="footer">
    <span>cabildodevenezuela.com</span>
  </div>
</div>
</body>
</html>"""


def generar_flyer_igpost(titulo: str, caption: str) -> str:
    """Flyer cuadrado 1:1 para Instagram Post."""
    t_esc  = _esc(titulo)
    # Separar hashtags del caption
    lineas = caption.strip().splitlines()
    hashtags_line = ""
    caption_lines = []
    for l in lineas:
        if l.strip().startswith("#"):
            hashtags_line = l.strip()
        else:
            caption_lines.append(l)
    cap_esc  = _esc("\n".join(caption_lines).strip())
    tags_esc = _esc(hashtags_line)
    logo     = _logo_data()

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flyer IG Post — {t_esc}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    width:1080px; height:1080px; overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:{_NAVY};
  }}
  .card {{
    width:100%; height:100%;
    background:linear-gradient(160deg,{_BLUE} 0%,{_NAVY} 50%,#071020 100%);
    display:flex; flex-direction:column;
    padding:80px 90px; position:relative; overflow:hidden;
  }}
  .deco-top {{
    position:absolute; top:0; right:0; width:480px; height:480px;
    background:radial-gradient(circle at top right,{_GOLD}22 0%,transparent 65%);
  }}
  .deco-bot {{
    position:absolute; bottom:0; left:0; width:360px; height:360px;
    background:radial-gradient(circle at bottom left,{_BLUE}88 0%,transparent 70%);
  }}
  .header {{
    display:flex; align-items:center; gap:18px; margin-bottom:auto;
    position:relative; z-index:1;
  }}
  .logo {{
    width:64px; height:64px; border-radius:50%; object-fit:cover;
    border:2px solid {_GOLD}50;
  }}
  .brand {{ display:flex; flex-direction:column; gap:2px; }}
  .brand-name {{ font-size:13px; font-weight:700; letter-spacing:0.18em; text-transform:uppercase; color:{_GOLD}; }}
  .brand-sub  {{ font-size:11px; color:rgba(255,255,255,0.4); letter-spacing:0.1em; text-transform:uppercase; }}
  .body {{ position:relative; z-index:1; }}
  .titulo {{
    font-size:52px; font-weight:800; color:{_WHITE};
    line-height:1.15; margin-bottom:28px;
    text-shadow:0 2px 20px rgba(0,0,0,0.4);
  }}
  .sep {{ width:50px; height:4px; background:{_GOLD}; border-radius:3px; margin-bottom:28px; }}
  .caption {{
    font-size:22px; color:rgba(255,255,255,0.78); line-height:1.7;
    white-space:pre-line;
  }}
  .tags {{
    margin-top:32px; font-size:15px; color:{_GOLD}88; font-weight:500; line-height:1.8;
  }}
  .footer {{
    position:relative; z-index:1; margin-top:auto; padding-top:32px;
    border-top:1px solid rgba(255,255,255,0.08);
    display:flex; justify-content:space-between; align-items:center;
  }}
  .footer-left {{ font-size:13px; color:rgba(255,255,255,0.3); letter-spacing:0.05em; }}
  .ig-badge {{
    background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.15);
    border-radius:20px; padding:6px 16px;
    font-size:12px; color:rgba(255,255,255,0.5); font-weight:600;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="deco-top"></div>
  <div class="deco-bot"></div>
  <div class="header">
    {'<img class="logo" src="' + logo + '" alt="Cabildo">' if logo else ''}
    <div class="brand">
      <div class="brand-name">Cabildo de Venezuela</div>
      <div class="brand-sub">Democracia participativa</div>
    </div>
  </div>
  <div class="body">
    <div class="titulo">{t_esc}</div>
    <div class="sep"></div>
    <div class="caption">{cap_esc}</div>
    {'<div class="tags">' + tags_esc + '</div>' if tags_esc else ''}
  </div>
  <div class="footer">
    <div class="footer-left">cabildodevenezuela.com</div>
    <div class="ig-badge">Instagram Post</div>
  </div>
</div>
</body>
</html>"""


def generar_flyer_igstory(titulo: str, texto_story: str) -> str:
    """Flyer vertical 9:16 para Instagram Story."""
    t_esc = _esc(titulo)
    s_esc = _esc(texto_story)
    logo  = _logo_data()

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flyer IG Story — {t_esc}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{
    width:1080px; height:1920px; overflow:hidden;
    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:{_NAVY};
  }}
  .card {{
    width:100%; height:100%;
    background:linear-gradient(175deg,{_BLUE} 0%,{_NAVY} 40%,#050d1a 100%);
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    padding:120px 80px; position:relative; overflow:hidden;
    text-align:center;
  }}
  .deco-1 {{
    position:absolute; width:800px; height:800px; border-radius:50%;
    background:radial-gradient(circle,{_GOLD}18 0%,transparent 65%);
    top:-200px; right:-200px;
  }}
  .deco-2 {{
    position:absolute; width:600px; height:600px; border-radius:50%;
    background:radial-gradient(circle,{_BLUE}cc 0%,transparent 70%);
    bottom:-150px; left:-150px;
  }}
  .top-bar {{
    position:absolute; top:60px; left:0; right:0;
    display:flex; flex-direction:column; align-items:center; gap:12px;
    z-index:2;
  }}
  .logo {{ width:80px; height:80px; border-radius:50%; object-fit:cover; border:2px solid {_GOLD}60; }}
  .brand {{ font-size:14px; font-weight:700; letter-spacing:0.2em; text-transform:uppercase; color:{_GOLD}; }}
  .center {{ position:relative; z-index:2; }}
  .eyebrow {{
    font-size:14px; font-weight:700; letter-spacing:0.18em; text-transform:uppercase;
    color:{_GOLD}; margin-bottom:32px;
  }}
  .titulo {{
    font-size:72px; font-weight:900; color:{_WHITE};
    line-height:1.1; margin-bottom:48px;
    text-shadow:0 4px 30px rgba(0,0,0,0.5);
  }}
  .sep {{ width:80px; height:4px; background:{_GOLD}; border-radius:3px; margin:0 auto 48px; }}
  .story-text {{
    font-size:36px; color:rgba(255,255,255,0.82); line-height:1.5;
    white-space:pre-line;
  }}
  .bottom-bar {{
    position:absolute; bottom:60px; left:0; right:0;
    display:flex; justify-content:center;
    z-index:2;
  }}
  .ig-badge {{
    background:rgba(255,255,255,0.08); border:1px solid rgba(255,255,255,0.15);
    border-radius:30px; padding:10px 28px;
    font-size:15px; color:rgba(255,255,255,0.45); font-weight:600;
    letter-spacing:0.05em;
  }}
</style>
</head>
<body>
<div class="card">
  <div class="deco-1"></div>
  <div class="deco-2"></div>
  <div class="top-bar">
    {'<img class="logo" src="' + logo + '" alt="Cabildo">' if logo else ''}
    <div class="brand">Cabildo de Venezuela</div>
  </div>
  <div class="center">
    <div class="eyebrow">Comunicado oficial</div>
    <div class="titulo">{t_esc}</div>
    <div class="sep"></div>
    <div class="story-text">{s_esc}</div>
  </div>
  <div class="bottom-bar">
    <div class="ig-badge">Instagram Story</div>
  </div>
</div>
</body>
</html>"""
