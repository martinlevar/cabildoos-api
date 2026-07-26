import logging
import os
import httpx
from fastapi import APIRouter, HTTPException, Depends, Header
from typing import Optional, List
from supabase import Client

from models.schemas import VerificationRecord, StatsResponse
from services.supabase_client import get_supabase

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
    Envía un email al usuario pidiendo más información.
    Body: { "mensaje": "..." }
    """
    mensaje = (body.get("mensaje") or "").strip()
    if not mensaje:
        raise HTTPException(status_code=400, detail="El mensaje no puede estar vacío")

    # Obtener email de contacto de la verificación
    try:
        res = supabase.table("verifications") \
            .select("contact_email, status") \
            .eq("id", vid) \
            .single() \
            .execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="Verificación no encontrada")
        contact_email = res.data.get("contact_email")
        if not contact_email:
            raise HTTPException(status_code=400, detail="El usuario no proporcionó email de contacto")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # Enviar email
    try:
        import asyncio
        await asyncio.to_thread(
            _enviar_email,
            contact_email,
            "CabildoOS — Tu solicitud de verificación necesita más información",
            mensaje,
        )
        supabase.table("verifications").update({
            "status": "info_requerida",
        }).eq("id", vid).execute()
        logger.info(f"Email enviado a {contact_email} para verificación {vid}")
        return {"ok": True, "enviado_a": contact_email}
    except ValueError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Error enviando email: {e}")
        raise HTTPException(status_code=500, detail=f"Error al enviar email: {e}")


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
