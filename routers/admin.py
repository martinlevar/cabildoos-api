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


def _verificar_admin(authorization: Optional[str] = Header(None)):
    """
    Verifica que el request viene de un admin autenticado via Supabase JWT.
    El frontend manda: Authorization: Bearer <supabase_access_token>
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    return authorization.split(" ")[1]


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

    # 2. Profiles — seat_number eliminado (Option B), usamos verification_id
    profiles_res = supabase.table("profiles").select("id, alias, verification_id, status").execute()
    profiles = {p["id"]: p for p in (profiles_res.data or [])}

    # 3. Combinar
    result = []
    for uid, u in auth_users.items():
        p = profiles.get(uid, {})
        status = "verificado" if p.get("verification_id") else (p.get("status") or "sin_verificar")
        result.append({
            "id":          uid,
            "email":       u["email"],
            "alias":       p.get("alias", u["email"].split("@")[0]),
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

    # 1. Obtener butaca del usuario antes de borrar nada
    try:
        profile_res = supabase.table("profiles") \
            .select("butaca_numero") \
            .eq("id", user_id) \
            .maybe_single() \
            .execute()
        butaca = profile_res.data.get("butaca_numero") if profile_res.data else None
    except Exception:
        butaca = None

    # 2. Si tenía butaca: purgar todo el historial de ese asiento
    if butaca:
        try:
            supabase.rpc("purge_seat_history", {"p_seat": butaca}).execute()
        except Exception as e:
            logger.warning(f"purge_seat_history falló para butaca {butaca}: {e}")

    # 3. Borrar el perfil explícitamente (no hay CASCADE en profiles → auth.users)
    try:
        supabase.table("profiles").delete().eq("id", user_id).execute()
    except Exception as e:
        logger.warning(f"Error borrando profile {user_id}: {e}")

    # 4. Borrar de auth via Admin API
    import httpx as _httpx
    resp = _httpx.delete(
        f"{project_url}/auth/v1/admin/users/{user_id}",
        headers={
            "Authorization": f"Bearer {service_key}",
            "apikey": service_key,
        },
        timeout=15,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Error Supabase Auth: {resp.text}",
        )

    return {"ok": True, "butaca_liberada": butaca}


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


def _enviar_email(to_email: str, subject: str, body: str):
    """
    Envía un email vía Resend (https://resend.com).
    Variable de entorno requerida: RESEND_API_KEY
    Variable opcional: RESEND_FROM (default: verificacion@cabildoos.com)
    """
    api_key  = os.environ.get("RESEND_API_KEY", "")
    from_    = os.environ.get("RESEND_FROM", "CabildoOS Verificación <verificacion@cabildoos.com>")

    if not api_key:
        raise ValueError("RESEND_API_KEY no configurada")

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#1a1a1a">
      <div style="background:#f76a1e;padding:24px;border-radius:10px 10px 0 0">
        <h2 style="margin:0;color:#fff;font-size:20px">◈ CabildoOS — Verificación de Identidad</h2>
      </div>
      <div style="background:#f9f9f9;padding:28px;border-radius:0 0 10px 10px;border:1px solid #eee;border-top:none">
        <p style="margin:0 0 16px;font-size:15px">{body.replace(chr(10), '<br>')}</p>
        <hr style="border:none;border-top:1px solid #eee;margin:20px 0">
        <p style="margin:0;font-size:12px;color:#888">
          Este mensaje fue enviado por el equipo de verificación de CabildoOS.<br>
          No respondas a este email — ingresá a
          <a href="https://cabildoos.pages.dev" style="color:#f76a1e">cabildoos.pages.dev</a>
          para reenviar tu solicitud.
        </p>
      </div>
    </div>
    """

    with httpx.Client(timeout=15) as client:
        resp = client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from":    from_,
                "to":      [to_email],
                "subject": subject,
                "html":    html_body,
                "text":    body,
            },
        )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Resend error {resp.status_code}: {resp.text}")


@router.post("/verifications/{vid}/contact")
async def contact_user(
    vid: str,
    body: dict,
    token: str = Depends(_verificar_admin),
    supabase: Client = Depends(get_supabase),
):
    """
    Pide más info al usuario:
    1. Guarda todo en contact_requests (audit trail)
    2. Envía el email
    3. Elimina el registro de verifications (libera el doc_hash para que pueda reverificarse)
    Body: { "mensaje": "..." }
    """
    import asyncio
    mensaje = (body.get("mensaje") or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # 1. Obtener todos los datos de la verificación antes de borrar
    try:
        res = supabase.table("verifications") \
            .select("id, contact_email, doc_face_url, selfie_doc_url, doc_match, doc_hash, status") \
            .eq("id", vid) \
            .single() \
            .execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Verificación no encontrada")
        v = res.data
        contact_email = v.get("contact_email")
        if not contact_email:
            raise HTTPException(status_code=400, detail="El usuario no proporcionó email de contacto")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # 2. Guardar en contact_requests (audit trail permanente)
    try:
        cr = supabase.table("contact_requests").insert({
            "verification_id": vid,
            "contact_email":   contact_email,
            "doc_face_url":    v.get("doc_face_url"),
            "selfie_doc_url":  v.get("selfie_doc_url"),
            "doc_match":       v.get("doc_match"),
            "doc_hash":        v.get("doc_hash"),
            "mensaje_inicial": mensaje,
            "status":          "esperando",
            "notas":           [],
        }).execute()
        cr_id = cr.data[0]["id"] if cr.data else None
        logger.info(f"contact_request creado: {cr_id}")
    except Exception as e:
        logger.error(f"Error creando contact_request: {e}")
        raise HTTPException(status_code=500, detail=f"Error guardando registro: {e}")

    # 3. Enviar email
    try:
        await asyncio.to_thread(
            _enviar_email,
            contact_email,
            "CabildoOS — Tu solicitud de verificación necesita más información",
            mensaje,
        )
        logger.info(f"Email enviado a {contact_email}")
    except Exception as e:
        # Si falla el email, no borramos el registro
        logger.error(f"Error enviando email: {e}")
        raise HTTPException(status_code=500, detail=f"Error al enviar email: {e}")

    # 4. Eliminar de verifications (libera el doc_hash para reverificación)
    try:
        supabase.table("verifications").delete().eq("id", vid).execute()
        logger.info(f"Verificación {vid} eliminada — doc_hash liberado")
    except Exception as e:
        logger.warning(f"Email enviado pero error al eliminar verificación: {e}")

    return {"ok": True, "enviado_a": contact_email, "contact_request_id": cr_id}


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
