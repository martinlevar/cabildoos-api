"""
services/gemini.py — Cliente Gemini para vocería y digest.

Inicializa el cliente con GEMINI_API_KEY del entorno.
Si la clave no está configurada el cliente queda en None y las funciones
que lo usan devuelven un fallback sin crashear.
"""

import os
import logging

logger = logging.getLogger(__name__)

# ── Configuración ───────────────────────────────────────────────────────────

_GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")

_gemini_client = None


def _init_client():
    global _gemini_client
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY no configurada — vocería/digest AI deshabilitados")
        return
    try:
        from google import genai
        _gemini_client = genai.Client(api_key=api_key)
        logger.info("Gemini client inicializado (model=%s)", _GEMINI_MODEL)
    except Exception as exc:
        logger.error("Error inicializando Gemini: %s", exc)


_init_client()


# ── Contadores en memoria (se resetean con cada deploy) ────────────────────

_stats: dict = {
    "calls": 0,
    "tokens_in": 0,
    "tokens_out": 0,
    "errors": 0,
}


def _record_call(tokens_in: int = 0, tokens_out: int = 0) -> None:
    _stats["calls"] += 1
    _stats["tokens_in"] += tokens_in
    _stats["tokens_out"] += tokens_out


def _record_error(msg: str = "") -> None:
    _stats["errors"] += 1
    logger.warning("Gemini error registrado: %s", msg)


def get_gemini_stats() -> dict:
    return {
        "status": "enabled" if _gemini_client else "disabled",
        "model": _GEMINI_MODEL,
        "calls": _stats["calls"],
        "tokens_in": _stats["tokens_in"],
        "tokens_out": _stats["tokens_out"],
        "errors": _stats["errors"],
    }
