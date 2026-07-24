import logging
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
