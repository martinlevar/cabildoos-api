import asyncio
import base64
import logging
import math
import os
from datetime import datetime, timezone, timedelta

import html as _html
from supabase import Client

logger = logging.getLogger(__name__)


def _esc(s) -> str:
    """Escape text to ASCII-safe HTML entities."""
    return _html.escape(str(s or ""), quote=False).encode("ascii", "xmlcharrefreplace").decode("ascii")

VE_OFFSET = timedelta(hours=-4)  # Venezuela UTC-4


def _rango_ayer_ve():
    """Returns (start_iso, end_iso, fecha_str) for yesterday in Venezuela time."""
    now_ve = datetime.now(timezone.utc) + VE_OFFSET
    ayer_ve = now_ve.date() - timedelta(days=1)
    ve_tz = timezone(VE_OFFSET)
    start = datetime(ayer_ve.year, ayer_ve.month, ayer_ve.day, tzinfo=ve_tz)
    end = start + timedelta(days=1)
    MESES = ["enero","febrero","marzo","abril","mayo","junio",
              "julio","agosto","septiembre","octubre","noviembre","diciembre"]
    fecha_str = f"{ayer_ve.day} de {MESES[ayer_ve.month - 1]} de {ayer_ve.year}"
    return start.isoformat(), end.isoformat(), fecha_str


def obtener_datos_ayer(supabase: Client) -> dict:
    start, end, fecha_str = _rango_ayer_ve()

    preguntas_res = supabase.table("questions") \
        .select("id, text, category, description, status, opens_at, closes_at") \
        .gte("ends_at", start) \
        .lt("ends_at", end) \
        .execute()
    preguntas = preguntas_res.data or []

    question_ids = [q["id"] for q in preguntas]
    votos, mensajes = [], []

    if question_ids:
        votos_res = supabase.table("votes") \
            .select("question_id, vote_plain") \
            .in_("question_id", question_ids) \
            .execute()
        votos = votos_res.data or []

        mensajes_res = supabase.table("debate_messages") \
            .select("question_id, seat_number, alias, text, created_at") \
            .in_("question_id", question_ids) \
            .order("created_at") \
            .execute()
        mensajes = mensajes_res.data or []

    propuestas_res = supabase.table("proposals") \
        .select("id, seat_number, text, cat, likes, status, created_at") \
        .gte("created_at", start) \
        .lt("created_at", end) \
        .execute()
    propuestas = propuestas_res.data or []

    return {
        "fecha_str": fecha_str,
        "preguntas": preguntas,
        "votos": votos,
        "mensajes": mensajes,
        "propuestas": propuestas,
    }


def obtener_emails_verificados(supabase: Client) -> list:
    """Devuelve todos los emails de usuarios con verificación aprobada.
    Usa la función SQL get_emails_verificados() que hace JOIN con auth.users
    para cubrir usuarios cuyo email no está en la tabla profiles."""
    res = supabase.rpc("get_emails_verificados").execute()
    return [row["email"] for row in (res.data or []) if row.get("email")]


def _votos_por_pregunta(votos: list) -> dict:
    resultado: dict = {}
    for v in votos:
        qid = v["question_id"]
        opcion = v.get("vote_plain") or "?"
        resultado.setdefault(qid, {})
        resultado[qid][opcion] = resultado[qid].get(opcion, 0) + 1
    return resultado


def _generar_resumen_gemini_sync(datos: dict) -> str:
    from services.gemini import _gemini_client, _GEMINI_MODEL, _record_call, _record_error

    client = _gemini_client
    if client is None:
        return "Resumen no disponible — Gemini no está inicializado."

    fecha_str = datos["fecha_str"]
    preguntas = datos["preguntas"]
    mensajes = datos["mensajes"]
    propuestas = datos["propuestas"]
    vpq = _votos_por_pregunta(datos["votos"])

    ctx = [f"Fecha: {fecha_str}"]

    if preguntas:
        ctx.append(f"\nPreguntas debatidas ({len(preguntas)}):")
        for q in preguntas:
            ctx.append(f"- [{q.get('category','')}] {q['text']}")
            qid = q["id"]
            if qid in vpq:
                total = sum(vpq[qid].values())
                dist = ", ".join(f"{k}: {v}" for k, v in sorted(vpq[qid].items(), key=lambda x: -x[1]))
                ctx.append(f"  Votos totales: {total} — distribución: {dist}")
    else:
        ctx.append("\nNo hubo preguntas debatidas ayer.")

    if mensajes:
        ctx.append(f"\nMensajes en el debate ({len(mensajes)}):")
        for m in mensajes[:12]:
            alias = m.get("alias") or f"Butaca {m.get('seat_number','?')}"
            ctx.append(f"  {alias}: {m['text'][:100]}")

    if propuestas:
        ctx.append(f"\nPropuestas ciudadanas ({len(propuestas)}):")
        for p in propuestas[:8]:
            ctx.append(f"- [{p.get('cat','')}] {p['text'][:120]} (apoyo: {p.get('likes',0)})")

    contexto = "\n".join(ctx)

    prompt = f"""Sos el secretario del Cabildo de Venezuela. Redactá un resumen narrativo EN ESPAÑOL de la actividad parlamentaria de ayer ({fecha_str}) para enviar por email a los ciudadanos verificados.

DATOS DE LA SESIÓN:
{contexto}

INSTRUCCIONES:
- Tono: formal pero cercano, como un diario de sesiones ciudadano
- Longitud: 3 a 5 párrafos
- Mencioná qué se debatió, los resultados de las votaciones y las propuestas más destacadas
- Si no hubo actividad, escribí un mensaje breve e inspirador invitando a la participación
- Solo texto plano con saltos de línea entre párrafos, sin markdown ni símbolos especiales
- Terminá con una invitación a participar en los próximos debates"""

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
        return response.text.strip()
    except Exception as e:
        _record_error(f"digest_gemini: {e}")
        logger.error(f"Gemini digest error: {e}")
        return "El resumen narrativo no pudo generarse en este momento."


async def generar_resumen_gemini(datos: dict) -> str:
    return await asyncio.to_thread(_generar_resumen_gemini_sync, datos)


def _arco_svg_bg() -> str:
    """Genera el arco de puntos multicolor como data URI.
    Centro en la base del header, semicírculos completos visibles."""
    W, H = 580, 260
    cx, cy = 290, H  # centro en la base → semicírculos suben hacia arriba
    colores = ["#e63946", "#f4a261", "#e9c46a", "#4ade80", "#60a5fa", "#a78bfa", "#e63946", "#f4a261"]
    parts = [f'<rect width="{W}" height="{H}" fill="#0a0f1e"/>']
    for i, radio in enumerate(range(28, 270, 27)):
        n_dots = max(14, int(math.pi * radio / 8))
        for j in range(n_dots):
            angle = math.pi + (j / max(n_dots - 1, 1)) * math.pi
            x = cx + radio * math.cos(angle)
            y = cy + radio * math.sin(angle)
            if -3 <= x <= W + 3 and -3 <= y <= H + 3:
                color = colores[(i * 3 + j) % len(colores)]
                r = 2.4
                parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" opacity="0.92"/>')
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">'
        + "".join(parts) + "</svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


LOGO_DATA = "https://cabildodevenezuela.com/logo-cabildo.jpg"
ARC_URL   = "https://cabildodevenezuela.com/email-arc.png"

def construir_email_html(datos: dict, resumen: str) -> str:
    preguntas  = datos["preguntas"]
    votos      = datos["votos"]
    mensajes   = datos["mensajes"]
    propuestas = datos["propuestas"]

    total_preguntas  = len(preguntas)
    total_votos      = len(votos)
    total_mensajes   = len(mensajes)
    total_propuestas = len(propuestas)

    fecha_esc = _esc(datos["fecha_str"])

    vpq = _votos_por_pregunta(votos)

    # Preguntas block
    preguntas_html = ""
    for q in preguntas:
        qid   = q["id"]
        cat   = _esc(q.get("category", ""))
        texto = _esc(q.get("text", ""))
        votos_q = vpq.get(qid, {})
        total_q = sum(votos_q.values())
        barras = ""
        for opcion, count in sorted(votos_q.items(), key=lambda x: -x[1]):
            pct = round(count / total_q * 100) if total_q else 0
            opcion_esc = _esc(opcion)
            opcion_lower = opcion.lower().strip()
            if opcion_lower in ("sí", "si", "yes"):
                bar_color = "#16a34a"
                label_color = "#15803d"
            elif opcion_lower in ("no",):
                bar_color = "#dc2626"
                label_color = "#b91c1c"
            elif "abstenci" in opcion_lower:
                bar_color = "#9ca3af"
                label_color = "#6b7280"
            else:
                bar_color = "#1e3a5f"
                label_color = "#1e3a5f"
            barras += f"""
            <div style="margin-bottom:10px;">
              <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
                <span style="font-size:13px;color:#374151;font-weight:500;">{opcion_esc}</span>
                <span style="font-size:13px;font-weight:700;color:{label_color};">{pct}% ({count})</span>
              </div>
              <div style="background:#f3f4f6;border-radius:999px;height:5px;overflow:hidden;">
                <div style="width:{pct}%;background:{bar_color};height:5px;border-radius:999px;"></div>
              </div>
            </div>"""
        if not barras:
            barras = '<p style="font-size:13px;color:#9ca3af;margin:0;">Sin votos registrados</p>'
        preguntas_html += f"""
        <div style="padding:20px 0;border-bottom:1px solid #f3f4f6;">
          <div style="font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#6b7280;margin-bottom:8px;">{cat}</div>
          <p style="font-size:15px;color:#111827;line-height:1.6;margin:0 0 14px 0;font-weight:500;">{texto}</p>
          <div style="font-size:12px;color:#9ca3af;margin-bottom:12px;">{total_q} votos registrados</div>
          {barras}
        </div>"""

    if not preguntas_html:
        preguntas_html = '<p style="font-size:14px;color:#9ca3af;margin:0;padding:16px 0;">No hubo preguntas debatidas ayer.</p>'

    # Propuestas block
    propuestas_html = ""
    for p in propuestas[:5]:
        cat_p   = _esc(p.get("cat", ""))
        texto_p = _esc(p.get("text", "")[:200])
        likes   = int(p.get("likes", 0) or 0)
        propuestas_html += f"""
        <div style="padding:16px 0;border-bottom:1px solid #f3f4f6;">
          <div style="font-size:10px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;color:#6b7280;margin-bottom:6px;">{cat_p}</div>
          <p style="font-size:14px;color:#374151;line-height:1.55;margin:0 0 8px 0;">{texto_p}</p>
          <span style="font-size:12px;color:#9ca3af;">&#128077; {likes} apoyos</span>
        </div>"""
    if not propuestas_html:
        propuestas_html = '<p style="font-size:14px;color:#9ca3af;margin:0;padding:16px 0;">No hubo propuestas ayer.</p>'

    # Resumen
    parrafos_html = "".join(
        f'<p style="font-size:15px;color:#374151;line-height:1.8;margin:0 0 16px 0;">{_esc(par.strip())}</p>'
        for par in resumen.split("\n") if par.strip()
    )

    ARC_BG = _arco_svg_bg()

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diario del Cabildo &mdash; {fecha_esc}</title>
</head>
<body style="margin:0;padding:0;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:40px 16px;">
<tr><td align="center">
<table width="580" cellpadding="0" cellspacing="0" style="max-width:580px;width:100%;">

  <!-- HEADER -->
  <tr><td style="background:#0a0f1e;background-image:url('{ARC_BG}');background-size:cover;background-position:center top;border-radius:16px 16px 0 0;padding:48px 48px 36px;text-align:center;border-bottom:1px solid #1e3a5f;">
    <img src="{LOGO_DATA}" width="120" height="120" alt="Cabildo de Venezuela" style="display:block;margin:0 auto 20px;border-radius:50%;object-fit:cover;">
    <div style="font-size:11px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:rgba(255,255,255,0.55);margin-bottom:8px;">Diario de Sesiones</div>
    <div style="font-size:14px;color:rgba(255,255,255,0.8);font-weight:500;">{fecha_esc}</div>
  </td></tr>

  <!-- STATS -->
  <tr><td style="background:#ffffff;padding:24px 48px;border-bottom:1px solid #f3f4f6;">
    <table width="100%" cellpadding="0" cellspacing="0"><tr>
      <td style="text-align:center;padding:0 4px;">
        <div style="font-size:28px;font-weight:700;color:#1e3a5f;letter-spacing:-0.02em;">{total_preguntas}</div>
        <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.1em;margin-top:4px;">Preguntas</div>
      </td>
      <td style="text-align:center;padding:0 4px;border-left:1px solid #f3f4f6;">
        <div style="font-size:28px;font-weight:700;color:#1e3a5f;letter-spacing:-0.02em;">{total_votos}</div>
        <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.1em;margin-top:4px;">Votos</div>
      </td>
      <td style="text-align:center;padding:0 4px;border-left:1px solid #f3f4f6;">
        <div style="font-size:28px;font-weight:700;color:#1e3a5f;letter-spacing:-0.02em;">{total_mensajes}</div>
        <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.1em;margin-top:4px;">Mensajes</div>
      </td>
      <td style="text-align:center;padding:0 4px;border-left:1px solid #f3f4f6;">
        <div style="font-size:28px;font-weight:700;color:#1e3a5f;letter-spacing:-0.02em;">{total_propuestas}</div>
        <div style="font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:0.1em;margin-top:4px;">Propuestas</div>
      </td>
    </tr></table>
  </td></tr>

  <!-- RESUMEN -->
  <tr><td style="background:#ffffff;padding:36px 48px;border-bottom:1px solid #f3f4f6;">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:#9ca3af;margin-bottom:20px;">Resumen de la Sesi&#243;n</div>
    {parrafos_html}
  </td></tr>

  <!-- PREGUNTAS -->
  <tr><td style="background:#ffffff;padding:36px 48px;border-bottom:1px solid #f3f4f6;">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">Preguntas y Votaciones</div>
    {preguntas_html}
  </td></tr>

  <!-- PROPUESTAS -->
  <tr><td style="background:#ffffff;padding:36px 48px;border-bottom:1px solid #f3f4f6;">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:#9ca3af;margin-bottom:4px;">Propuestas Ciudadanas</div>
    {propuestas_html}
  </td></tr>

  <!-- CTA -->
  <tr><td style="background:#ffffff;padding:36px 48px 48px;text-align:center;border-radius:0 0 16px 16px;">
    <p style="font-size:14px;color:#6b7280;margin:0 0 20px 0;">Tu voz construye la democracia venezolana.</p>
    <a href="https://cabildodevenezuela.com" style="display:inline-block;background:#1e3a5f;color:#ffffff;font-weight:600;font-size:13px;text-decoration:none;padding:14px 36px;border-radius:8px;letter-spacing:0.04em;">Ingresar al Cabildo &#8594;</a>
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="padding:24px 48px;text-align:center;">
    <p style="font-size:12px;color:#9ca3af;line-height:1.7;margin:0;">
      Recib&#237;s este email porque sos un ciudadano verificado en CabildoOS.<br>
      <a href="https://cabildodevenezuela.com" style="color:#6b7280;text-decoration:none;">cabildodevenezuela.com</a>
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


async def enviar_digest(supabase: Client) -> dict:
    """Main entry point: fetch data, generate summary, send emails."""
    import resend as resend_sdk

    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get(
        "RESEND_FROM",
        "Cabildo de Venezuela <digest@cabildodevenezuela.com>"
    )

    if not api_key:
        raise ValueError("RESEND_API_KEY no configurada")

    resend_sdk.api_key = api_key

    # 1. Fetch yesterday's data
    datos = await asyncio.to_thread(obtener_datos_ayer, supabase)
    logger.info(
        f"Digest [{datos['fecha_str']}]: {len(datos['preguntas'])} preguntas, "
        f"{len(datos['votos'])} votos, {len(datos['propuestas'])} propuestas"
    )

    # 2. Gemini narrative
    resumen = await generar_resumen_gemini(datos)

    # 3. Build HTML
    html = construir_email_html(datos, resumen)

    # 4. Get verified user emails
    emails = await asyncio.to_thread(obtener_emails_verificados, supabase)
    logger.info(f"Digest: enviando a {len(emails)} usuarios verificados")

    if not emails:
        return {
            "ok": True,
            "enviados": 0,
            "fecha": datos["fecha_str"],
            "nota": "No hay usuarios verificados con email",
        }

    # 5. Send via Resend
    asunto = f"Diario del Cabildo — {datos['fecha_str']}"
    enviados = 0
    errores = []

    for email in emails:
        try:
            resend_sdk.Emails.send({
                "from": from_email,
                "to": [email],
                "subject": asunto,
                "html": html,
            })
            enviados += 1
        except Exception as e:
            errores.append(str(e))
            logger.error(f"Error enviando digest a {email}: {e}")

    return {
        "ok": True,
        "enviados": enviados,
        "errores": len(errores),
        "fecha": datos["fecha_str"],
        "total_destinatarios": len(emails),
    }


async def enviar_a_un_email(supabase: Client, email: str) -> dict:
    """Envía el digest solo a un email específico (para pruebas)."""
    import resend as resend_sdk

    api_key = os.environ.get("RESEND_API_KEY", "")
    from_email = os.environ.get("RESEND_FROM", "digest@cabildodevenezuela.com")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY no configurado")
    resend_sdk.api_key = api_key

    datos = await asyncio.to_thread(obtener_datos_ayer, supabase)
    resumen = await generar_resumen_gemini(datos)
    html = construir_email_html(datos, resumen)
    asunto = f"Diario del Cabildo — {datos['fecha_str']}"

    resend_sdk.Emails.send({
        "from": from_email,
        "to": [email],
        "subject": asunto,
        "html": html,
    })

    return {"ok": True, "enviado_a": email, "fecha": datos["fecha_str"]}
