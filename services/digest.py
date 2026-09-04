import asyncio
import logging
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
            barras += f"""
            <div style="margin-bottom:10px;">
              <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
                <span style="font-size:13px;color:#374151;">{opcion_esc}</span>
                <span style="font-size:13px;font-weight:600;color:#1e3a5f;">{pct}%  ({count})</span>
              </div>
              <div style="background:#f3f4f6;border-radius:999px;height:4px;overflow:hidden;">
                <div style="width:{pct}%;background:#1e3a5f;height:4px;border-radius:999px;"></div>
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

    LOGO_DATA = "data:image/jpeg;base64,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"

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
  <tr><td style="background:#ffffff;border-radius:16px 16px 0 0;padding:48px 48px 32px;text-align:center;border-bottom:1px solid #f3f4f6;">
    <img src="{LOGO_DATA}" width="80" height="80" alt="Cabildo de Venezuela" style="border-radius:50%;display:block;margin:0 auto 20px;">
    <div style="font-size:11px;font-weight:700;letter-spacing:0.18em;text-transform:uppercase;color:#9ca3af;margin-bottom:8px;">Diario de Sesiones</div>
    <div style="font-size:13px;color:#6b7280;">{fecha_esc}</div>
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
