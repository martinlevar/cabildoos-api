"""
Router del Playroom — Nerdmocracy y Yo, Presidente.
Prefijo: /api/playroom

Seguridad:
  - Todos los endpoints requieren JWT válido de Supabase (cualquier usuario autenticado).
  - correct_index de Nerdmocracy NUNCA se envía al cliente; solo se almacena
    en nerdmocracy_questions_cache con RLS habilitado y cero políticas de cliente
    (solo service_role puede leerlo).
  - Las respuestas se validan 100% server-side.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from supabase import Client

from services.supabase_client import get_supabase
from services.playroom_gemini import (
    generar_pregunta_nerdmocracy,
    generar_escenario_yopresidente,
    generar_consecuencia_yopresidente,
)

router = APIRouter(prefix="/api/playroom", tags=["playroom"])
logger = logging.getLogger(__name__)

_SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
_SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


# ── Auth ───────────────────────────────────────────────────────────────────────

def _verificar_usuario(authorization: Optional[str] = Header(None)) -> dict:
    """
    Valida el Bearer JWT contra Supabase /auth/v1/user.
    Cualquier usuario autenticado puede usar el Playroom.
    Devuelve el dict de usuario de Supabase.
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
        raise HTTPException(status_code=401, detail=f"Error verificando token: {exc}")

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

    user_data = resp.json()
    if not user_data.get("id"):
        raise HTTPException(status_code=401, detail="Token sin user_id")

    return user_data


def _check_playroom_active(supabase: Client) -> None:
    """Lanza 403 si el Playroom está desactivado en system_config."""
    try:
        res = (
            supabase.table("system_config")
            .select("value")
            .eq("key", "playroom_active")
            .single()
            .execute()
        )
        if res.data:
            val = res.data.get("value")
            # val puede ser bool False o string "false"
            if val is False or val == "false" or val is None:
                raise HTTPException(status_code=403, detail="El Playroom está cerrado en este momento")
    except HTTPException:
        raise
    except Exception:
        # Si no podemos leer system_config, no bloqueamos al usuario
        pass


# ══════════════════════════════════════════════════════════════════════════════
# NERDMOCRACY
# ══════════════════════════════════════════════════════════════════════════════

class AnswerRequest(BaseModel):
    session_id:   str
    question_id:  str
    answer_index: int   # 0-3


@router.post("/nerdmocracy/session/start")
async def nerdmocracy_start_session(
    user:     dict   = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """
    Crea una nueva sesión de Nerdmocracy para el usuario.
    Debe llamarse antes de pedir la primera pregunta.
    """
    _check_playroom_active(supabase)
    user_id = user["id"]

    try:
        res = (
            supabase.table("nerdmocracy_sessions")
            .insert({"user_id": user_id, "score": 0})
            .execute()
        )
        session_id = res.data[0]["id"]
    except Exception as exc:
        logger.error("Error creando sesión Nerdmocracy user=%s: %s", user_id, exc)
        raise HTTPException(status_code=503, detail="Error de base de datos")

    return {"session_id": session_id}


@router.post("/nerdmocracy/question")
async def nerdmocracy_get_question(
    user:     dict   = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """
    Genera una pregunta de trivia vía Gemini y la almacena en caché server-side.

    ► El campo `correct_index` se guarda en `nerdmocracy_questions_cache`
      con RLS habilitado y cero políticas de cliente → solo service_role puede leerlo.
    ► Al cliente se le devuelve: question_id, question_text, options.
      correct_index NUNCA sale del servidor.
    """
    _check_playroom_active(supabase)
    user_id = user["id"]

    try:
        q = await generar_pregunta_nerdmocracy()
    except Exception as exc:
        logger.error("Error Gemini generando pregunta Nerdmocracy: %s", exc)
        raise HTTPException(status_code=503, detail="Error generando pregunta, intenta de nuevo")

    # Almacenar con service_role — correct_index solo accesible server-side
    try:
        res = (
            supabase.table("nerdmocracy_questions_cache")
            .insert({
                "user_id":       user_id,
                "question_text": q["question_text"],
                "options":       q["options"],
                "correct_index": q["correct_index"],   # SECRET — no sale en el return
            })
            .execute()
        )
        question_id = res.data[0]["id"]
    except Exception as exc:
        logger.error("Error guardando pregunta en caché user=%s: %s", user_id, exc)
        raise HTTPException(status_code=503, detail="Error de base de datos")

    # ► Retorno al cliente — SIN correct_index
    return {
        "question_id":   question_id,
        "question_text": q["question_text"],
        "options":       q["options"],
    }


@router.post("/nerdmocracy/answer")
async def nerdmocracy_submit_answer(
    body:     AnswerRequest,
    user:     dict          = Depends(_verificar_usuario),
    supabase: Client        = Depends(get_supabase),
):
    """
    Valida la respuesta del usuario 100% server-side.

    Flujo:
      1. Lee correct_index de la caché (service_role — el cliente nunca lo tuvo).
      2. Verifica que la pregunta pertenezca a este usuario.
      3. Si correcta  → incrementa score en la sesión.
      4. Si incorrecta → graba ended_at (timestamp tie-breaker), game_over = True.

    El campo `correct_index` NO aparece en ninguna respuesta al cliente.
    """
    _check_playroom_active(supabase)
    user_id = user["id"]

    if body.answer_index not in (0, 1, 2, 3):
        raise HTTPException(status_code=400, detail="answer_index debe ser 0, 1, 2 o 3")

    # ── 1. Obtener correct_index de la caché (solo service_role puede leer esta tabla) ──
    try:
        cache_res = (
            supabase.table("nerdmocracy_questions_cache")
            .select("correct_index, user_id")
            .eq("id", body.question_id)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Pregunta no encontrada o expirada")

    if not cache_res.data:
        raise HTTPException(status_code=404, detail="Pregunta no encontrada")

    cache_row = cache_res.data

    # ── 2. La pregunta debe pertenecer a este usuario ──
    if cache_row["user_id"] != user_id:
        logger.warning(
            "Intento de respuesta cruzada: user=%s pregunta de user=%s",
            user_id, cache_row["user_id"],
        )
        raise HTTPException(status_code=403, detail="Pregunta no pertenece a este usuario")

    correct_index = cache_row["correct_index"]
    is_correct    = (body.answer_index == correct_index)

    # ── 3. Verificar sesión ──
    try:
        session_res = (
            supabase.table("nerdmocracy_sessions")
            .select("id, score, ended_at")
            .eq("id", body.session_id)
            .eq("user_id", user_id)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    if not session_res.data:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    session = session_res.data

    if session.get("ended_at"):
        raise HTTPException(status_code=409, detail="Esta sesión ya terminó")

    current_score = session.get("score", 0)

    # ── 4. Actualizar sesión ──
    if is_correct:
        new_score = current_score + 1
        supabase.table("nerdmocracy_sessions").update({"score": new_score}).eq(
            "id", body.session_id
        ).eq("user_id", user_id).execute()

        return {"correct": True, "game_over": False, "score": new_score}

    else:
        # Game over — grabar ended_at para tie-breaking (el primero en llegar gana)
        ended_at = datetime.now(timezone.utc).isoformat()
        supabase.table("nerdmocracy_sessions").update({
            "ended_at": ended_at,
            "score":    current_score,
        }).eq("id", body.session_id).eq("user_id", user_id).execute()

        return {"correct": False, "game_over": True, "score": current_score}


# ══════════════════════════════════════════════════════════════════════════════
# YO, PRESIDENTE
# ══════════════════════════════════════════════════════════════════════════════

_YOP_INITIAL_METERS = {"energia": 70, "capital_politico": 70, "salud_mental": 70}


class DecisionRequest(BaseModel):
    decision_index: int    # 0-2
    crisis_text:    str    # La crisis que se le mostró al usuario
    option_text:    str    # La opción que eligió


@router.get("/yopresidente/state")
async def yopresidente_get_state(
    user:     dict   = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """Devuelve el estado actual del juego del usuario."""
    _check_playroom_active(supabase)
    user_id = user["id"]

    try:
        res = (
            supabase.table("yopresidente_state")
            .select("day, energia, capital_politico, salud_mental, game_over, history")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        d = res.data
    except Exception:
        d = None

    if not d:
        return {
            "exists": False,
            "day":              1,
            **_YOP_INITIAL_METERS,
            "game_over":        False,
            "history":          [],
        }

    return {
        "exists":            True,
        "day":               d["day"],
        "energia":           d["energia"],
        "capital_politico":  d["capital_politico"],
        "salud_mental":      d["salud_mental"],
        "game_over":         d.get("game_over", False),
        "history":           d.get("history", []),
    }


@router.post("/yopresidente/scenario")
async def yopresidente_get_scenario(
    user:     dict   = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """
    Genera el escenario del día actual vía Gemini.
    Si el usuario no tiene estado de juego, lo inicializa.
    """
    _check_playroom_active(supabase)
    user_id = user["id"]

    # Leer estado actual
    try:
        state_res = (
            supabase.table("yopresidente_state")
            .select("*")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        state = state_res.data
    except Exception:
        state = None

    if state and state.get("game_over"):
        raise HTTPException(
            status_code=409,
            detail="El juego terminó. Usa /yopresidente/reset para empezar de nuevo.",
        )

    # Inicializar si no existe
    if not state:
        try:
            init_res = (
                supabase.table("yopresidente_state")
                .insert({
                    "user_id":          user_id,
                    "day":              1,
                    **_YOP_INITIAL_METERS,
                    "game_over":        False,
                    "history":          [],
                })
                .execute()
            )
            state = init_res.data[0]
        except Exception as exc:
            logger.error("Error inicializando YoPresidente user=%s: %s", user_id, exc)
            raise HTTPException(status_code=503, detail="Error de base de datos")

    dia     = state.get("day", 1)
    historia = state.get("history", [])

    try:
        scenario = await generar_escenario_yopresidente(dia, historia)
    except Exception as exc:
        logger.error("Error Gemini escenario YoPresidente día=%s: %s", dia, exc)
        raise HTTPException(status_code=503, detail="Error generando escenario, intenta de nuevo")

    return {
        "day":         dia,
        "crisis_text": scenario["crisis_text"],
        "options":     scenario["options"],
        "current_meters": {
            "energia":          state.get("energia",          70),
            "capital_politico": state.get("capital_politico", 70),
            "salud_mental":     state.get("salud_mental",     70),
        },
    }


@router.post("/yopresidente/decision")
async def yopresidente_submit_decision(
    body:     DecisionRequest,
    user:     dict            = Depends(_verificar_usuario),
    supabase: Client          = Depends(get_supabase),
):
    """
    Procesa la decisión del presidente:
      1. Genera consecuencia narrativa vía Gemini
      2. Aplica deltas a los medidores (0-100)
      3. Detecta game over si algún medidor llega a 0
      4. Actualiza yopresidente_state
      5. Loguea en yopresidente_decisions (append-only)
    """
    _check_playroom_active(supabase)
    user_id = user["id"]

    if body.decision_index not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="decision_index debe ser 0, 1 o 2")

    # Obtener estado actual
    try:
        state_res = (
            supabase.table("yopresidente_state")
            .select("day, energia, capital_politico, salud_mental, game_over, history")
            .eq("user_id", user_id)
            .single()
            .execute()
        )
        state = state_res.data
    except Exception:
        raise HTTPException(
            status_code=404,
            detail="Estado no encontrado. Llama primero a /yopresidente/scenario.",
        )

    if not state:
        raise HTTPException(status_code=404, detail="Estado no encontrado")

    if state.get("game_over"):
        raise HTTPException(status_code=409, detail="El juego ya terminó")

    energia  = state.get("energia",          70)
    capital  = state.get("capital_politico", 70)
    salud    = state.get("salud_mental",     70)
    dia      = state.get("day",              1)
    historia = state.get("history",          [])

    # ── Generar consecuencia vía Gemini ──
    try:
        resultado = await generar_consecuencia_yopresidente(
            dia=dia,
            crisis_text=body.crisis_text,
            decision_text=body.option_text,
            energia=energia,
            capital_politico=capital,
            salud_mental=salud,
        )
    except Exception as exc:
        logger.error("Error Gemini consecuencia día=%s user=%s: %s", dia, user_id, exc)
        raise HTTPException(status_code=503, detail="Error generando consecuencia, intenta de nuevo")

    # ── Aplicar deltas (clamp 0-100) ──
    nueva_energia  = max(0, min(100, energia + resultado["delta_energia"]))
    nuevo_capital  = max(0, min(100, capital + resultado["delta_capital"]))
    nueva_salud    = max(0, min(100, salud   + resultado["delta_salud"]))
    game_over      = any(m == 0 for m in (nueva_energia, nuevo_capital, nueva_salud))

    # ── Actualizar historial (últimos 30 días) ──
    nueva_historia = historia + [{
        "day":         dia,
        "decision":    body.option_text[:100],
        "consequence": resultado["consequence_text"][:180],
    }]
    if len(nueva_historia) > 30:
        nueva_historia = nueva_historia[-30:]

    nuevo_dia = dia + 1

    # ── Persistir estado ──
    try:
        supabase.table("yopresidente_state").update({
            "day":              nuevo_dia,
            "energia":          nueva_energia,
            "capital_politico": nuevo_capital,
            "salud_mental":     nueva_salud,
            "game_over":        game_over,
            "history":          nueva_historia,
        }).eq("user_id", user_id).execute()
    except Exception as exc:
        logger.error("Error actualizando estado YoPresidente user=%s: %s", user_id, exc)
        raise HTTPException(status_code=503, detail="Error de base de datos")

    # ── Log de decisión (best-effort, no falla el request) ──
    try:
        supabase.table("yopresidente_decisions").insert({
            "user_id":          user_id,
            "day":              dia,
            "crisis_text":      body.crisis_text[:380],
            "decision_index":   body.decision_index,
            "decision_text":    body.option_text[:180],
            "consequence_text": resultado["consequence_text"][:380],
            "delta_energia":    resultado["delta_energia"],
            "delta_capital":    resultado["delta_capital"],
            "delta_salud":      resultado["delta_salud"],
        }).execute()
    except Exception as exc:
        logger.warning("Error logueando decisión (no crítico) user=%s: %s", user_id, exc)

    return {
        "consequence_text": resultado["consequence_text"],
        "meter_deltas": {
            "energia":          resultado["delta_energia"],
            "capital_politico": resultado["delta_capital"],
            "salud_mental":     resultado["delta_salud"],
        },
        "new_state": {
            "day":              nuevo_dia,
            "energia":          nueva_energia,
            "capital_politico": nuevo_capital,
            "salud_mental":     nueva_salud,
        },
        "game_over": game_over,
    }


@router.post("/yopresidente/reset")
async def yopresidente_reset(
    user:     dict   = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """Reinicia el juego Yo, Presidente para el usuario (upsert al estado inicial)."""
    _check_playroom_active(supabase)
    user_id = user["id"]

    try:
        supabase.table("yopresidente_state").upsert(
            {
                "user_id":          user_id,
                "day":              1,
                **_YOP_INITIAL_METERS,
                "game_over":        False,
                "history":          [],
            },
            on_conflict="user_id",
        ).execute()
    except Exception as exc:
        logger.error("Error reseteando YoPresidente user=%s: %s", user_id, exc)
        raise HTTPException(status_code=503, detail="Error de base de datos")

    return {"ok": True, "message": "Juego reiniciado. ¡Buena suerte, Presidente!"}
