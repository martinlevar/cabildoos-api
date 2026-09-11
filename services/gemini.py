"""
services/gemini.py — Stub de compatibilidad.

Gemini fue removido de Nerdocrasy (las preguntas las provee el admin).
Este stub mantiene las importaciones que aún usan digest.py y voceria.py
para que el servidor arranque sin errores.

TODO: migrar digest y vocería a otro proveedor de AI o eliminarlos.
"""

import logging

logger = logging.getLogger(__name__)

# ── Stubs de los símbolos que importan digest.py y voceria.py ──────────────

_gemini_client = None
_GEMINI_MODEL = ""


def _record_call():
    pass


def _record_error(msg: str = ""):
    logger.warning("Gemini está deshabilitado — %s", msg)


# ── Stats que expone /api/admin/status/gemini ──────────────────────────────

def get_gemini_stats() -> dict:
    return {
        "status": "disabled",
        "calls": 0,
        "errors": 0,
        "note": "Gemini fue removido. digest y voceria requieren migración.",
    }
