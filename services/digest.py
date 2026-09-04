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
    profiles_res = supabase.table("profiles") \
        .select("email, verification_id") \
        .not_.is_("verification_id", "null") \
        .not_.is_("email", "null") \
        .execute()

    profiles = profiles_res.data or []
    if not profiles:
        return []

    ver_ids = [p["verification_id"] for p in profiles if p.get("verification_id")]
    if not ver_ids:
        return []

    aprobadas_res = supabase.table("verifications") \
        .select("id") \
        .in_("id", ver_ids) \
        .eq("status", "aprobado") \
        .execute()

    aprobadas_ids = {v["id"] for v in (aprobadas_res.data or [])}

    return list({
        p["email"]
        for p in profiles
        if p.get("verification_id") in aprobadas_ids and p.get("email")
    })


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


LOGO_DATA = "data:image/png;base64,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"

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
    <img src="{LOGO_DATA}" width="120" height="120" alt="Cabildo de Venezuela" style="display:block;margin:0 auto 20px;">
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
