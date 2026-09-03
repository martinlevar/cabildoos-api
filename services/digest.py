import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta

from supabase import Client

logger = logging.getLogger(__name__)

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
        .gte("opens_at", start) \
        .lt("opens_at", end) \
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
    fecha_str = datos["fecha_str"]
    preguntas = datos["preguntas"]
    votos = datos["votos"]
    propuestas = datos["propuestas"]
    mensajes = datos["mensajes"]

    total_votos = len(votos)
    total_preguntas = len(preguntas)
    total_mensajes = len(mensajes)
    total_propuestas = len(propuestas)
    vpq = _votos_por_pregunta(votos)

    # Preguntas block
    preguntas_html = ""
    for q in preguntas:
        qid = q["id"]
        cat = q.get("category", "")
        texto = q["text"]
        votos_q = vpq.get(qid, {})
        total_q = sum(votos_q.values())

        barras = ""
        for opcion, count in sorted(votos_q.items(), key=lambda x: -x[1]):
            pct = round(count / total_q * 100) if total_q else 0
            barras += f"""
              <div style="margin-bottom:8px;">
                <div style="display:flex;justify-content:space-between;font-size:13px;color:#334155;margin-bottom:3px;">
                  <span>{opcion}</span><span style="font-weight:600;">{count} ({pct}%)</span>
                </div>
                <div style="background:#e2e8f0;border-radius:4px;height:7px;">
                  <div style="background:#C9A84C;border-radius:4px;height:7px;width:{pct}%;"></div>
                </div>
              </div>"""

        if not barras:
            barras = '<p style="color:#94a3b8;font-size:13px;margin:0;">Sin votos registrados</p>'

        preguntas_html += f"""
        <div style="background:#f8fafc;border-left:4px solid #C9A84C;border-radius:0 8px 8px 0;padding:18px;margin-bottom:16px;">
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#C9A84C;margin-bottom:6px;">{cat}</div>
          <div style="font-size:15px;font-weight:600;color:#1e293b;margin-bottom:12px;line-height:1.4;">{texto}</div>
          <div style="font-size:12px;color:#64748b;margin-bottom:10px;">Total: <strong>{total_q}</strong> votos</div>
          {barras}
        </div>"""

    if not preguntas_html:
        preguntas_html = '<p style="color:#94a3b8;font-size:14px;margin:0;">No hubo preguntas debatidas ayer.</p>'

    # Propuestas block
    propuestas_html = ""
    for p in propuestas[:5]:
        cat = p.get("cat", "")
        texto = p["text"][:200]
        likes = p.get("likes", 0)
        propuestas_html += f"""
        <div style="border:1px solid #e2e8f0;border-radius:8px;padding:14px;margin-bottom:10px;">
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.08em;color:#C9A84C;margin-bottom:4px;">{cat}</div>
          <div style="font-size:14px;color:#1e293b;line-height:1.5;margin-bottom:8px;">{texto}</div>
          <div style="font-size:12px;color:#64748b;">&#128077; {likes} apoyos</div>
        </div>"""

    if not propuestas_html:
        propuestas_html = '<p style="color:#94a3b8;font-size:14px;margin:0;">No hubo propuestas ciudadanas ayer.</p>'

    # Resumen paragraphs
    resumen_html = "".join(
        f'<p style="color:#334155;font-size:15px;line-height:1.75;margin:0 0 14px 0;">{p.strip()}</p>'
        for p in resumen.split("\n") if p.strip()
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Diario del Cabildo &mdash; {fecha_str}</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:'Helvetica Neue',Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

  <!-- Header -->
  <tr><td style="background:#1e3a5f;border-radius:12px 12px 0 0;padding:36px 40px;text-align:center;">
    <div style="font-size:10px;font-weight:700;letter-spacing:0.15em;color:#C9A84C;text-transform:uppercase;margin-bottom:10px;">Cabildo de Venezuela</div>
    <div style="font-size:28px;font-weight:700;color:#ffffff;margin-bottom:6px;">Diario de Sesiones</div>
    <div style="font-size:14px;color:#94a3b8;letter-spacing:0.02em;">{fecha_str}</div>
  </td></tr>

  <!-- Stats bar -->
  <tr><td style="background:#17304f;padding:18px 40px;">
    <table width="100%" cellpadding="0" cellspacing="0">
    <tr>
      <td style="text-align:center;border-right:1px solid #2d4a6e;padding:0 8px;">
        <div style="font-size:22px;font-weight:700;color:#C9A84C;">{total_preguntas}</div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Preguntas</div>
      </td>
      <td style="text-align:center;border-right:1px solid #2d4a6e;padding:0 8px;">
        <div style="font-size:22px;font-weight:700;color:#C9A84C;">{total_votos}</div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Votos</div>
      </td>
      <td style="text-align:center;border-right:1px solid #2d4a6e;padding:0 8px;">
        <div style="font-size:22px;font-weight:700;color:#C9A84C;">{total_mensajes}</div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Mensajes</div>
      </td>
      <td style="text-align:center;padding:0 8px;">
        <div style="font-size:22px;font-weight:700;color:#C9A84C;">{total_propuestas}</div>
        <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.06em;margin-top:2px;">Propuestas</div>
      </td>
    </tr>
    </table>
  </td></tr>

  <!-- Body -->
  <tr><td style="background:#ffffff;padding:36px 40px;">

    <!-- Resumen narrativo -->
    <div style="margin-bottom:32px;">
      <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#C9A84C;margin-bottom:14px;padding-bottom:10px;border-bottom:2px solid #f1f5f9;">Resumen de la Sesi&#243;n</div>
      {resumen_html}
    </div>

    <!-- Preguntas y votos -->
    <div style="margin-bottom:32px;">
      <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#C9A84C;margin-bottom:14px;padding-bottom:10px;border-bottom:2px solid #f1f5f9;">Preguntas y Votaciones</div>
      {preguntas_html}
    </div>

    <!-- Propuestas -->
    <div style="margin-bottom:32px;">
      <div style="font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;color:#C9A84C;margin-bottom:14px;padding-bottom:10px;border-bottom:2px solid #f1f5f9;">Propuestas Ciudadanas</div>
      {propuestas_html}
    </div>

    <!-- CTA -->
    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:24px;text-align:center;">
      <div style="font-size:15px;font-weight:600;color:#1e3a5f;margin-bottom:10px;">&#191;Quer&#233;s participar en el pr&#243;ximo debate?</div>
      <div style="font-size:13px;color:#64748b;margin-bottom:16px;">Tu voz construye la democracia venezolana.</div>
      <a href="https://cabildodevenezuela.com" style="display:inline-block;background:#1e3a5f;color:#C9A84C;font-weight:700;font-size:13px;text-decoration:none;padding:12px 32px;border-radius:6px;letter-spacing:0.05em;">INGRESAR AL CABILDO &#8594;</a>
    </div>

  </td></tr>

  <!-- Footer -->
  <tr><td style="background:#f8fafc;border-top:1px solid #e2e8f0;border-radius:0 0 12px 12px;padding:20px 40px;text-align:center;">
    <div style="font-size:12px;color:#94a3b8;line-height:1.7;">
      Recib&#237;s este email porque sos un ciudadano verificado en CabildoOS.<br>
      <a href="https://cabildodevenezuela.com" style="color:#1e3a5f;text-decoration:none;font-weight:500;">cabildodevenezuela.com</a>
    </div>
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
        "DIGEST_FROM_EMAIL",
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
