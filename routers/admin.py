import logging
import os
import httpx
from fastapi import APIRouter, HTTPException, Depends, Header
from typing import Optional, List
from supabase import Client

from models.schemas import VerificationRecord, StatsResponse
from services.supabase_client import get_supabase
from services.gemini import get_gemini_stats

router = APIRouter(prefix="/api/admin", tags=["admin"])
logger = logging.getLogger(__name__)


_SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
_SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def _verificar_admin(
    authorization: Optional[str] = Header(None),
) -> str:
    """
    Verifica que el request viene de un master autenticado.
    Llama directamente a /auth/v1/user de Supabase via httpx —
    funciona con HS256 y ECC (P-256) sin depender de supabase-py.
    Un token válido de usuario regular recibe 403, no 401.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")

    token = authorization.split(" ")[1]

    try:
        resp = httpx.get(
            f"{_SUPABASE_URL}/auth/v1/user",
            headers={
                "Authorization": f"Bearer {token}",
                "apikey": _SUPABASE_SERVICE_KEY,
            },
            timeout=10,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Error al verificar token: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    user_data = resp.json()
    app_metadata = user_data.get("app_metadata") or {}

    if not app_metadata.get("is_master"):
        logger.warning(
            "Acceso denegado a /api/admin — user_id=%s email=%s",
            user_data.get("id"), user_data.get("email"),
        )
        raise HTTPException(status_code=403, detail="Acceso denegado: se requiere rol master")

    return token


@router.get("/status/gemini")
async def gemini_status(token: str = Depends(_verificar_admin)):
    """Métricas de uso de Gemini AI (contadores en memoria desde el último deploy)."""
    return get_gemini_stats()


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Estadísticas generales del dashboard de admin."""
    try:
        res = supabase.table("verifications").select(
            "status, created_at"
        ).execute()
        rows = res.data or []

        from datetime import datetime, timedelta, timezone
        ahora = datetime.now(timezone.utc)
        hoy = ahora.date()
        semana = ahora - timedelta(days=7)

        total     = len(rows)
        pending   = sum(1 for r in rows if r["status"] in ("pendiente_revision", "en_proceso"))
        approved  = sum(1 for r in rows if r["status"] in ("auto_aprobado", "aprobado"))
        rejected  = sum(1 for r in rows if r["status"] == "rechazado")
        hoy_count = sum(
            1 for r in rows
            if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).date() == hoy
        )
        semana_count = sum(
            1 for r in rows
            if datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) >= semana
        )

        return StatsResponse(
            total=total, pending=pending, approved=approved, rejected=rejected,
            hoy=hoy_count, esta_semana=semana_count,
        )
    except Exception as e:
        logger.error(f"Error obteniendo stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/users")
async def list_users(
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Lista todos los usuarios registrados con su estado de verificación.
    Usa la Admin API de Supabase (requiere SUPABASE_SERVICE_ROLE_KEY).
    """
    import os
    project_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    if not service_key:
        raise HTTPException(status_code=503, detail="SUPABASE_SERVICE_ROLE_KEY no configurada")

    # 1. Obtener todos los auth.users via Admin API
    auth_users = {}
    try:
        page = 1
        while True:
            resp = httpx.get(
                f"{project_url}/auth/v1/admin/users?page={page}&per_page=1000",
                headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
                timeout=15,
            )
            data = resp.json()
            batch = data.get("users", [])
            for u in batch:
                app_meta = u.get("app_metadata") or {}
                # Excluir Masters, Observers y Revocados (no son ciudadanos del cabildo)
                if app_meta.get("is_master") or app_meta.get("role") in ("observer", "revoked"):
                    continue
                auth_users[u["id"]] = {
                    "id": u["id"],
                    "email": u.get("email", ""),
                    "created_at": u.get("created_at", ""),
                    "confirmed": bool(u.get("email_confirmed_at")),
                    "last_sign_in": u.get("last_sign_in_at"),
                    "is_master": False,
                }
            if len(batch) < 1000:
                break
            page += 1
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error consultando auth users: {e}")

    # 2. Profiles — alias y butaca_numero fueron migrados a seat_identities (privacidad)
    profiles_res = supabase.table("profiles").select("id, verification_id, status").execute()
    profiles = {p["id"]: p for p in (profiles_res.data or [])}

    # 3. Combinar
    result = []
    for uid, u in auth_users.items():
        p = profiles.get(uid, {})
        status = "verificado" if p.get("verification_id") else (p.get("status") or "sin_verificar")
        result.append({
            "id":          uid,
            "email":       u["email"],
            "alias":       u["email"].split("@")[0],
            "created_at":  u["created_at"],
            "confirmed":   u["confirmed"],
            "last_sign_in": u["last_sign_in"],
            "verified":       bool(p.get("verification_id")),
            "status":         status,
            "account_status": p.get("status", "active"),
        })

    result.sort(key=lambda x: x["created_at"], reverse=True)
    return result


@router.post("/users/{uid}/remind")
async def remind_user(
    uid: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Envía un email de recordatorio al usuario para que complete su verificación.
    Body: { "email": "...", "mensaje": "..." (opcional) }
    """
    import asyncio
    email   = (body.get("email") or "").strip()
    alias   = (body.get("alias") or "usuario").strip()
    mensaje = (body.get("mensaje") or "").strip()

    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")

    cuerpo = mensaje or f"""Hola {alias},

Te escribimos desde CabildoOS para recordarte que aún no completaste tu verificación de identidad.

La verificación es rápida (menos de 2 minutos) y te permite participar en el hemiciclo democrático como ciudadano verificado.

Para verificarte, ingresá a cabildoos.pages.dev y hacé clic en "Verificar identidad".

¡Te esperamos!"""

    try:
        await asyncio.to_thread(
            _enviar_email,
            email,
            "CabildoOS — Completá tu verificación de identidad",
            cuerpo,
        )
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Elimina permanentemente un usuario de Supabase Auth y libera su butaca.
    Orden: purge_seat_history → eliminar profile → eliminar auth user
    """
    project_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        raise HTTPException(status_code=503, detail="SUPABASE_SERVICE_ROLE_KEY no configurada")

    # Delegar toda la limpieza al RPC admin_delete_user:
    # borra seat_identities (libera butaca), verifications (libera DNI),
    # verification_requests, profiles y auth.users en un solo paso atómico.
    try:
        supabase.rpc("admin_delete_user", {"p_user_id": user_id}).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error eliminando usuario: {e}")

    return {"ok": True}


@router.post("/users/{user_id}/block-doc")
async def block_doc_and_delete_user(
    user_id: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Elimina al usuario Y bloquea permanentemente su doc_hash.
    El DNI bloqueado no podrá usarse para crear ninguna cuenta nueva.
    Body: { "admin_email": "..." (opcional) }
    """
    project_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not service_key:
        raise HTTPException(status_code=503, detail="SUPABASE_SERVICE_ROLE_KEY no configurada")

    admin_email = (body.get("admin_email") or "").strip()

    # 1. Obtener doc_hash via verification_id en profiles
    doc_hash = None
    try:
        profile_res = supabase.table("profiles") \
            .select("verification_id") \
            .eq("id", user_id) \
            .maybe_single() \
            .execute()
        verification_id = profile_res.data.get("verification_id") if profile_res.data else None

        if verification_id:
            ver_res = supabase.table("verifications") \
                .select("doc_hash") \
                .eq("id", verification_id) \
                .maybe_single() \
                .execute()
            doc_hash = ver_res.data.get("doc_hash") if ver_res.data else None
    except Exception as e:
        logger.warning(f"Error obteniendo doc_hash de {user_id}: {e}")

    # 2. Bloquear el doc_hash si existe
    if doc_hash:
        try:
            supabase.table("blocked_doc_hashes").upsert({
                "hash":       doc_hash,
                "blocked_by": admin_email or "admin",
                "reason":     "Bloqueado al eliminar usuario",
            }).execute()
            logger.info(f"doc_hash bloqueado para usuario {user_id}")
        except Exception as e:
            logger.warning(f"Error bloqueando doc_hash: {e}")

    # Delegar la eliminación completa al RPC (libera butaca + DNI + historial)
    # El doc_hash ya quedó copiado a blocked_doc_hashes arriba — queda bloqueado
    # incluso si el RPC borra la verificación original.
    try:
        supabase.rpc("admin_delete_user", {"p_user_id": user_id}).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error eliminando usuario: {e}")

    return {
        "ok": True,
        "doc_hash_bloqueado": bool(doc_hash),
    }


@router.get("/verifications", response_model=List[VerificationRecord])
async def list_verifications(
    status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Lista verifications con filtro opcional por status."""
    try:
        q = supabase.table("verifications").select("*").order(
            "created_at", desc=True
        ).range(offset, offset + limit - 1)
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/verifications/{vid}", response_model=VerificationRecord)
async def get_verification(
    vid: str,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Detalle de una verificación específica."""
    try:
        res = supabase.table("verifications").select("*").eq("id", vid).single().execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="No encontrado")
        return res.data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _html_aprobacion(butaca: int) -> str:
    """Template HTML profesional para el email de aprobación de butaca."""
    site_url = "https://cabildodevenezuela.com"
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="X-UA-Compatible" content="IE=edge">
  <title>Tu butaca en CabildoOS</title>
</head>
<body style="margin:0;padding:0;background-color:#0f1117;font-family:Arial,Helvetica,sans-serif;">

  <!-- Preheader oculto (preview en inbox) -->
  <div style="display:none;max-height:0;overflow:hidden;mso-hide:all;">
    Tu identidad fue verificada. Ocupas la butaca #{butaca} en el hemiciclo de CabildoOS.
    &nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#0f1117;">
    <tr>
      <td align="center" style="padding:32px 16px;">

        <!-- Contenedor principal -->
        <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;">

          <!-- Header naranja -->
          <tr>
            <td style="background-color:#f76a1e;border-radius:12px 12px 0 0;padding:28px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td>
                    <p style="margin:0;color:#fff;font-size:13px;letter-spacing:2px;text-transform:uppercase;opacity:0.85;">CabildoOS</p>
                    <h1 style="margin:6px 0 0;color:#fff;font-size:24px;font-weight:700;line-height:1.2;">
                      Identidad verificada
                    </h1>
                  </td>
                  <td align="right" style="font-size:40px;line-height:1;">◈</td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Cuerpo -->
          <tr>
            <td style="background-color:#1a1d27;padding:36px 32px;border-left:1px solid #2a2d3a;border-right:1px solid #2a2d3a;">

              <p style="margin:0 0 20px;color:#e0e0e0;font-size:16px;line-height:1.6;">
                Tu identidad fue verificada por el equipo de CabildoOS.
                Ya tenés tu lugar en el hemiciclo.
              </p>

              <!-- Tarjeta de butaca -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="background-color:#0f1117;border:1px solid #f76a1e;border-radius:10px;padding:24px;text-align:center;">
                    <p style="margin:0 0 4px;color:#f76a1e;font-size:11px;letter-spacing:2px;text-transform:uppercase;">Tu butaca</p>
                    <p style="margin:0;color:#fff;font-size:52px;font-weight:700;line-height:1;">#{butaca}</p>
                    <p style="margin:8px 0 0;color:#888;font-size:13px;">Hemiciclo · CabildoOS</p>
                  </td>
                </tr>
              </table>

              <p style="margin:24px 0 28px;color:#aaa;font-size:14px;line-height:1.7;">
                Este lugar es tuyo en el debate democrático. Ingresá al Cabildo para ver tu butaca
                en el hemiciclo, seguir delegados, y participar en las propuestas.
              </p>

              <!-- CTA -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center">
                    <a href="{site_url}"
                       style="display:inline-block;background-color:#f76a1e;color:#fff;
                              font-size:15px;font-weight:700;text-decoration:none;
                              padding:14px 40px;border-radius:8px;letter-spacing:0.5px;">
                      Ir al Cabildo →
                    </a>
                  </td>
                </tr>
              </table>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background-color:#13161f;border-radius:0 0 12px 12px;padding:20px 32px;
                       border:1px solid #2a2d3a;border-top:none;">
              <p style="margin:0;color:#555;font-size:12px;line-height:1.6;text-align:center;">
                Este mensaje fue enviado por el equipo de CabildoOS.<br>
                Si no iniciaste este proceso, podés ignorar este email.<br>
                <a href="{site_url}" style="color:#f76a1e;text-decoration:none;">cabildodevenezuela.com</a>
              </p>
            </td>
          </tr>

        </table>
        <!-- /Contenedor principal -->

      </td>
    </tr>
  </table>

</body>
</html>"""


def _text_aprobacion(butaca: int) -> str:
    return f"""Tu identidad fue verificada por el equipo de CabildoOS.

BUTACA #{butaca}

Este lugar es tuyo en el hemiciclo. Ingresá al Cabildo para ver tu butaca,
seguir delegados y participar en las propuestas.

https://cabildodevenezuela.com

---
CabildoOS · Si no iniciaste este proceso, ignorá este email."""


def _html_to_text(html: str) -> str:
    """Genera versión texto plano desde HTML — elimina tags y decodifica entidades."""
    import re, html as _html_mod
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</(?:p|div|tr|li|h\d)[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = _html_mod.unescape(text)
    # Colapsar múltiples líneas en blanco
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _enviar_email(to_email: str, subject: str, body_text: str = "", body_html: str | None = None):
    """
    Envía un email vía Resend (https://resend.com).
    Variables de entorno:
      RESEND_API_KEY  — requerida
      RESEND_FROM     — opcional (default: Cabildo de Venezuela <hola@cabildodevenezuela.com>)
      RESEND_REPLY_TO — opcional (default: igual que RESEND_FROM)

    IMPORTANTE para evitar spam:
      - El dominio del FROM debe estar verificado en Resend con SPF + DKIM configurados.
      - En Resend > Domains: agregar cabildodevenezuela.com y copiar los registros DNS.
    """
    api_key   = os.environ.get("RESEND_API_KEY", "")
    from_     = os.environ.get("RESEND_FROM", "Cabildo de Venezuela <hola@cabildodevenezuela.com>")
    reply_to  = os.environ.get("RESEND_REPLY_TO", from_)

    if not api_key:
        raise ValueError("RESEND_API_KEY no configurada")

    # Si no se pasa HTML, generar uno básico desde el texto plano
    if body_html is None:
        body_html = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;color:#222;max-width:560px;margin:0 auto;padding:24px">
<pre style="white-space:pre-wrap;font-family:Arial,sans-serif">{body_text}</pre>
</body></html>"""

    # Si no se pasa texto plano, generarlo desde el HTML (crítico para Gmail Primary)
    if not body_text:
        body_text = _html_to_text(body_html)

    payload = {
        "from":     from_,
        "reply_to": reply_to,
        "to":       [to_email],
        "subject":  subject,
        "html":     body_html,
        "text":     body_text,
        "headers": {
            # ID único por destinatario — evita que Gmail agrupe envíos como campaña
            "X-Entity-Ref-ID": f"cabildoos-{to_email}",
            # Indica a los clientes de correo que es una lista de distribución
            "Precedence": "list",
        },
    }

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend error {resp.status_code}: {resp.text}")


@router.get("/verifications/{vid}/contact-email")
async def get_verification_contact_email(
    vid: str,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Obtiene el email de contacto de una verificación pendiente, bajo demanda.
    Resuelve user_id → auth.users en memoria — NO almacena la conexión en ningún lado.
    """
    import httpx, os
    try:
        res = supabase.table("verifications") \
            .select("user_id") \
            .eq("id", vid) \
            .single() \
            .execute()
        if not res.data or not res.data.get("user_id"):
            raise HTTPException(status_code=404, detail="No hay usuario vinculado a esta verificación")
        user_id = res.data["user_id"]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    supabase_url = os.environ.get("SUPABASE_URL", "")
    service_key  = os.environ.get("SUPABASE_SERVICE_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            auth_resp = await client.get(
                f"{supabase_url}/auth/v1/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
            )
        if auth_resp.status_code == 200:
            email = auth_resp.json().get("email")
            if email:
                return {"email": email}
        raise HTTPException(status_code=404, detail="No se encontró email para este usuario")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verifications/{vid}/contact")
async def contact_user(
    vid: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Pide más info al usuario:
    1. Resuelve el email en memoria (user_id → auth.users) — sin almacenar la conexión
    2. Envía el email
    3. Elimina el registro de verifications (libera el doc_hash para reverificación)

    PRIVACIDAD: no queda ningún rastro que vincule butaca/cara con email.
    Body: { "mensaje": "..." }
    """
    import asyncio, httpx as _httpx, os as _os
    mensaje = (body.get("mensaje") or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # 1. Obtener solo user_id — sin cargar fotos ni doc_hash
    try:
        res = supabase.table("verifications") \
            .select("user_id") \
            .eq("id", vid) \
            .single() \
            .execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Verificación no encontrada")
        user_id = res.data.get("user_id")
        if not user_id:
            raise HTTPException(status_code=400, detail="Esta verificación no tiene usuario vinculado")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 2. Resolver email en memoria via auth.users — nunca se persiste esta conexión
    supabase_url = _os.environ.get("SUPABASE_URL", "")
    service_key  = _os.environ.get("SUPABASE_SERVICE_KEY", "")
    contact_email: str | None = None
    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            auth_resp = await client.get(
                f"{supabase_url}/auth/v1/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
            )
        if auth_resp.status_code == 200:
            contact_email = auth_resp.json().get("email")
    except Exception:
        pass

    if not contact_email:
        raise HTTPException(status_code=400, detail="No se encontró email para este usuario")

    # 3. Enviar email con template HTML
    site_url = "https://cabildodevenezuela.com"
    html_contacto = f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#0f1117;font-family:Arial,Helvetica,sans-serif;">
<div style="display:none;max-height:0;overflow:hidden;">El equipo de CabildoOS necesita más información para completar tu verificación.&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#0f1117;">
  <tr><td align="center" style="padding:32px 16px;">
    <table width="560" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;width:100%;">
      <tr><td style="background-color:#7c3aed;border-radius:12px 12px 0 0;padding:28px 32px;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
          <td><p style="margin:0;color:#fff;font-size:13px;letter-spacing:2px;text-transform:uppercase;opacity:.85;">CabildoOS</p>
              <h1 style="margin:6px 0 0;color:#fff;font-size:22px;font-weight:700;">Necesitamos más información</h1></td>
          <td align="right" style="font-size:36px;line-height:1;">📧</td>
        </tr></table>
      </td></tr>
      <tr><td style="background-color:#1a1d27;padding:32px;border-left:1px solid #2a2d3a;border-right:1px solid #2a2d3a;">
        <p style="margin:0 0 16px;color:#aaa;font-size:14px;line-height:1.6;">El equipo de verificación revisó tu solicitud y tiene una consulta:</p>
        <div style="background:#0f1117;border-left:3px solid #a855f7;border-radius:4px;padding:16px 20px;margin:0 0 24px;">
          <p style="margin:0;color:#e0e0e0;font-size:15px;line-height:1.7;white-space:pre-wrap;">{mensaje}</p>
        </div>
        <p style="margin:0 0 24px;color:#aaa;font-size:14px;line-height:1.6;">
          Podés volver a iniciar el proceso de verificación con la información solicitada.
          Tu documento previo fue liberado — podés usarlo nuevamente.
        </p>
        <table width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">
          <a href="{site_url}" style="display:inline-block;background-color:#7c3aed;color:#fff;font-size:15px;font-weight:700;text-decoration:none;padding:14px 40px;border-radius:8px;">
            Ir a CabildoOS →
          </a>
        </td></tr></table>
      </td></tr>
      <tr><td style="background-color:#13161f;border-radius:0 0 12px 12px;padding:20px 32px;border:1px solid #2a2d3a;border-top:none;">
        <p style="margin:0;color:#555;font-size:12px;line-height:1.6;text-align:center;">
          Este mensaje fue enviado por el equipo de verificación de CabildoOS.<br>
          <a href="{site_url}" style="color:#a855f7;text-decoration:none;">cabildodevenezuela.com</a>
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""
    try:
        await asyncio.to_thread(
            _enviar_email,
            contact_email,
            "Tu verificación en CabildoOS necesita un ajuste",
            mensaje,
            html_contacto,
        )
        logger.info("Email de contacto enviado — verificación será eliminada")
    except Exception as e:
        logger.error(f"Error enviando email: {e}")
        raise HTTPException(status_code=500, detail=f"Error al enviar email: {e}")

    # 4. Eliminar de verifications (libera el doc_hash para reverificación)
    #    PRIVACIDAD: al borrar esto desaparece la última conexión entre cara y usuario.
    try:
        supabase.table("verifications").delete().eq("id", vid).execute()
        logger.info(f"Verificación {vid} eliminada — sin rastro")
    except Exception as e:
        logger.warning(f"Email enviado pero error al eliminar verificación: {e}")

    return {"ok": True}


@router.get("/contact-requests")
async def list_contact_requests(
    status: Optional[str] = None,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Lista todos los pedidos de más información con su historial."""
    try:
        q = supabase.table("contact_requests") \
            .select("*") \
            .order("created_at", desc=True)
        if status:
            q = q.eq("status", status)
        res = q.execute()
        return res.data or []
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/contact-requests/{crid}/nota")
async def agregar_nota(
    crid: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Agrega una nota de seguimiento a un contact_request."""
    from datetime import datetime, timezone
    texto = (body.get("texto") or "").strip()
    if not texto:
        raise HTTPException(status_code=400, detail="Nota vacía")
    try:
        res = supabase.table("contact_requests").select("notas").eq("id", crid).single().execute()
        notas = res.data.get("notas") or []
        notas.append({"texto": texto, "ts": datetime.now(timezone.utc).isoformat()})
        supabase.table("contact_requests").update({
            "notas":      notas,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", crid).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch("/contact-requests/{crid}/status")
async def actualizar_status_cr(
    crid: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Actualiza el status de un contact_request (aprobado, rechazado, cerrado, etc.)."""
    from datetime import datetime, timezone
    new_status = body.get("status")
    if new_status not in ("esperando", "re_enviado", "aprobado", "rechazado", "cerrado"):
        raise HTTPException(status_code=400, detail="Status inválido")
    try:
        supabase.table("contact_requests").update({
            "status":     new_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", crid).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/verifications/{vid}/approve")
async def approve_verification(
    vid: str,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Aprueba una verificación:
    1. Llama assign_butaca() para asignar el próximo número de butaca
    2. Envía email de confirmación al usuario con su butaca asignada
    Retorna: { butaca, email_sent, email }
    """
    import asyncio
    project_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

    # 1. Llamar assign_butaca via RPC
    try:
        result = supabase.rpc("assign_butaca", {"p_verification_id": vid}).execute()
        butaca = result.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error asignando butaca: {e}")

    if not butaca:
        raise HTTPException(status_code=500, detail="assign_butaca no retornó número de butaca")

    # 2. Borrar imágenes de verificación (privacidad — ya cumplieron su función)
    try:
        supabase.storage.from_("verifications").remove([
            f"{vid}/selfie_documento.jpg",
            f"{vid}/documento.jpg",
        ])
    except Exception as e:
        logger.warning(f"No se pudieron borrar imágenes de {vid}: {e}")

    # 3. Obtener email del usuario para notificar (siempre via user_id → auth.users)
    user_email = None
    try:
        ver_res = supabase.table("verifications").select("user_id").eq("id", vid).maybe_single().execute()
        user_id = ver_res.data.get("user_id") if ver_res.data else None
        if user_id and service_key:
            auth_resp = httpx.get(
                f"{project_url}/auth/v1/admin/users/{user_id}",
                headers={"Authorization": f"Bearer {service_key}", "apikey": service_key},
                timeout=10,
            )
            if auth_resp.status_code == 200:
                user_email = auth_resp.json().get("email")
    except Exception as e:
        logger.warning(f"No se pudo obtener email para verificación {vid}: {e}")

    # 4. Enviar email de aprobación
    email_sent = False
    if user_email:
        try:
            await asyncio.to_thread(
                _enviar_email,
                user_email,
                f"Tu butaca #{butaca} en CabildoOS ya está activa",
                _text_aprobacion(butaca),
                _html_aprobacion(butaca),
            )
            email_sent = True
            logger.info(f"Email de aprobación enviado a {user_email} — butaca #{butaca}")
        except Exception as e:
            logger.warning(f"No se pudo enviar email de aprobación: {e}")

    return {"ok": True, "butaca": butaca, "email_sent": email_sent, "email": user_email}


@router.patch("/verifications/{vid}/review")
async def review_verification(
    vid: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Admin aprueba o rechaza una verificación.
    Body: { "status": "aprobado" | "rechazado", "notes": "..." }
    """
    new_status = body.get("status")
    if new_status not in ("aprobado", "rechazado"):
        raise HTTPException(status_code=400, detail="Status inválido")

    from datetime import datetime, timezone
    try:
        supabase.table("verifications").update({
            "status": new_status,
            "review_notes": body.get("notes", ""),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", vid).execute()
        return {"ok": True, "status": new_status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/errors")
async def get_system_errors(
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
    limit: int = 100,
    error_type: Optional[str] = None,
    severity: Optional[str] = None,
):
    """
    Errores críticos del sistema guardados en system_errors.
    Filtrables por tipo (gemini, verification, auth) y severidad (warning, error, critical).
    """
    try:
        q = supabase.table("system_errors").select("*").order("created_at", desc=True).limit(limit)
        if error_type:
            q = q.eq("error_type", error_type)
        if severity:
            q = q.eq("severity", severity)
        res = q.execute()
        return {"ok": True, "errors": res.data or [], "total": len(res.data or [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/errors/{error_id}")
async def delete_system_error(
    error_id: str,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Marcar un error como resuelto / eliminarlo del panel."""
    try:
        supabase.table("system_errors").delete().eq("id", error_id).execute()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Secretaría / Digest ────────────────────────────────────────────────────

import asyncio as _asyncio
from services.digest import (
    obtener_datos_ayer as _obtener_datos_ayer,
    generar_resumen_gemini as _generar_resumen_gemini,
    construir_email_html as _construir_email_html,
    obtener_emails_verificados as _obtener_emails_verificados,
)


@router.get("/digest/datos")
async def digest_datos(
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Carga datos del digest del día anterior + resumen Gemini + HTML preview."""
    datos = await _asyncio.to_thread(_obtener_datos_ayer, supabase)
    resumen = await _generar_resumen_gemini(datos)
    html = _construir_email_html(datos, resumen)
    return {
        "fecha_str": datos["fecha_str"],
        "resumen": resumen,
        "html": html,
        "stats": {
            "preguntas": len(datos["preguntas"]),
            "votos": len(datos["votos"]),
            "mensajes": len(datos["mensajes"]),
            "propuestas": len(datos["propuestas"]),
        },
    }


@router.post("/digest/preview-custom")
async def digest_preview_custom(
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Reconstruye el HTML con un resumen editado por el admin."""
    datos = await _asyncio.to_thread(_obtener_datos_ayer, supabase)
    html = _construir_email_html(datos, body.get("resumen", ""))
    return {"html": html}


@router.post("/digest/enviar")
async def digest_enviar_admin(
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Envía el digest a todos los usuarios verificados usando el resumen aprobado."""
    datos = await _asyncio.to_thread(_obtener_datos_ayer, supabase)
    html = _construir_email_html(datos, body.get("resumen", ""))
    asunto = f"Diario del Cabildo — {datos['fecha_str']}"
    emails = body.get("emails_override") or await _asyncio.to_thread(_obtener_emails_verificados, supabase)

    enviados, errores = 0, []
    for email in emails:
        try:
            await _asyncio.to_thread(_enviar_email, email, asunto, "", html)
            enviados += 1
        except Exception as e:
            errores.append(str(e))
            logger.error(f"Error enviando digest a {email}: {e}")

    # Guardar en historial
    try:
        supabase.table("comunicados").insert({
            "tipo": "digest",
            "titulo": asunto,
            "html": html,
            "enviados": enviados,
            "errores": len(errores),
        }).execute()
    except Exception as e:
        logger.error(f"Error guardando comunicado en historial: {e}")

    return {"ok": True, "enviados": enviados, "errores": len(errores),
            "fecha": datos["fecha_str"], "total_destinatarios": len(emails)}


# ── Vocería del Cabildo ────────────────────────────────────────────────────

from services.voceria import (
    generar_anuncio_gemini as _generar_anuncio_gemini,
    construir_email_anuncio as _construir_email_anuncio,
)


@router.post("/voceria/generar")
async def voceria_generar(
    body: dict,
    token: str = Depends(_verificar_admin),
):
    """Recibe el prompt del admin y devuelve título + texto generado por Gemini + HTML preview."""
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Falta el campo 'prompt'")
    resultado = await _generar_anuncio_gemini(prompt)
    html = _construir_email_anuncio(resultado["titulo"], resultado["texto"])
    return {
        "titulo": resultado["titulo"],
        "texto": resultado["texto"],
        "html": html,
    }


@router.post("/voceria/preview-custom")
async def voceria_preview_custom(
    body: dict,
    token: str = Depends(_verificar_admin),
):
    """Reconstruye el HTML con título y texto editados por el admin."""
    titulo = body.get("titulo", "Comunicado del Cabildo")
    texto = body.get("texto", "")
    html = _construir_email_anuncio(titulo, texto)
    return {"html": html}


@router.post("/voceria/enviar")
async def voceria_enviar(
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Envía el comunicado a todos los usuarios verificados (o a emails_override)."""
    titulo = body.get("titulo", "Comunicado del Cabildo")
    texto = body.get("texto", "")
    if not texto.strip():
        raise HTTPException(status_code=400, detail="El texto del comunicado no puede estar vacío")

    html = _construir_email_anuncio(titulo, texto)
    asunto = titulo
    emails = body.get("emails_override") or await _asyncio.to_thread(_obtener_emails_verificados, supabase)

    enviados, errores = 0, []
    for email in emails:
        try:
            await _asyncio.to_thread(_enviar_email, email, asunto, "", html)
            enviados += 1
        except Exception as e:
            errores.append(str(e))
            logger.error(f"Error enviando vocería a {email}: {e}")

    # Guardar en historial
    try:
        supabase.table("comunicados").insert({
            "tipo": "voceria",
            "titulo": titulo,
            "html": html,
            "enviados": enviados,
            "errores": len(errores),
        }).execute()
    except Exception as e:
        logger.error(f"Error guardando comunicado en historial: {e}")

    return {"ok": True, "enviados": enviados, "errores": len(errores),
            "total_destinatarios": len(emails)}


# ── Historial de comunicados ───────────────────────────────────────────────

@router.get("/comunicados")
async def listar_comunicados(
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Lista los últimos 100 comunicados enviados."""
    res = supabase.table("comunicados") \
        .select("id, tipo, titulo, enviados, errores, created_at") \
        .order("created_at", desc=True) \
        .limit(100) \
        .execute()
    return res.data or []


@router.get("/comunicados/{comunicado_id}/html")
async def get_comunicado_html(
    comunicado_id: str,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """Devuelve el HTML de un comunicado para previsualización o PDF."""
    from fastapi.responses import HTMLResponse
    res = supabase.table("comunicados") \
        .select("html, titulo") \
        .eq("id", comunicado_id) \
        .single() \
        .execute()
    if not res.data:
        raise HTTPException(status_code=404, detail="Comunicado no encontrado")
    return HTMLResponse(content=res.data["html"])
