"""
Logging centralizado de errores críticos a Supabase.
El backend usa service_role key → bypasea RLS → puede escribir en system_errors.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


def log_error(
    supabase,
    error_type: str,
    message: str,
    severity: str = "error",
    details: Optional[dict] = None,
    ip: Optional[str] = None,
    user_id: Optional[str] = None,
):
    """
    Guarda un error crítico en system_errors.
    No lanza excepciones — el logging nunca debe romper el flujo principal.

    error_type: 'gemini' | 'verification' | 'auth' | 'storage' | 'general'
    severity:   'warning' | 'error' | 'critical'
    """
    try:
        row = {
            "error_type": error_type,
            "severity":   severity,
            "message":    message,
        }
        if details:
            row["details"] = details
        if ip:
            row["ip"] = ip
        if user_id:
            row["user_id"] = user_id

        supabase.table("system_errors").insert(row).execute()
    except Exception as e:
        # Si falla el log, lo registramos localmente pero no propagamos
        logger.warning(f"error_log.log_error falló al escribir en DB: {e}")


def log_gemini_error(supabase, model: str, error_msg: str, ip: Optional[str] = None):
    """Shorthand para errores de Gemini API."""
    is_model_unavailable = "no longer available" in error_msg or "404" in error_msg
    severity = "critical" if is_model_unavailable else "error"
    log_error(
        supabase,
        error_type="gemini",
        severity=severity,
        message=f"Gemini API error ({model}): {error_msg[:300]}",
        details={"model": model, "raw_error": error_msg[:500]},
        ip=ip,
    )


def log_verification_failure(
    supabase,
    reason: str,
    tipo_doc: str,
    campos_fallidos: list[str],
    ip: Optional[str] = None,
    confianza: Optional[float] = None,
):
    """Shorthand para fallos de verificación de documentos."""
    log_error(
        supabase,
        error_type="verification",
        severity="warning",
        message=f"Verificación fallida — {reason}",
        details={
            "tipo_doc":       tipo_doc,
            "campos_fallidos": campos_fallidos,
            "confianza":       confianza,
        },
        ip=ip,
    )
