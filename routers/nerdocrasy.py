"""
Router de Nerdocrasy — /api/nerdocrasy
Reemplaza el viejo router de Playroom (Nerdmocracy + Yo, Presidente).

Seguridad:
  - Todos los endpoints requieren JWT válido de Supabase.
  - correct_answer NUNCA se envía al cliente; solo se usa server-side en submit_answer.
  - El timer es controlado por el servidor (current_question_deadline).
  - Una sola respuesta por pregunta por intento (idempotency check).
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
from services.nerdocrasy_engine import (
    level_to_stage,
    question_deadline,
    is_within_deadline,
    select_question,
    get_question_for_client,
    record_exposure,
    update_best_level,
)

router = APIRouter(prefix="/api/nerdocrasy", tags=["nerdocrasy"])
logger = logging.getLogger(__name__)

_SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
_SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


# ── Auth ───────────────────────────────────────────────────────────────────────

def _verificar_usuario(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token requerido")
    token = authorization.split(" ")[1]
    try:
        resp = httpx.get(
            f"{_SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": _SUPABASE_SERVICE_KEY},
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


def _get_seat_id(supabase: Client, user_id: str) -> Optional[int]:
    """Obtiene la butaca del ciudadano (puede ser None si no está verificado)."""
    try:
        hmac_resp = supabase.rpc("get_my_seat").execute()
        return hmac_resp.data if hmac_resp.data else None
    except Exception:
        return None


def _cancel_active_attempts(supabase: Client, user_id: str) -> None:
    """Cancela cualquier intento activo previo al iniciar uno nuevo."""
    supabase.table("nerdocrasy_attempts").update({
        "is_active":      False,
        "finished_at":    datetime.now(timezone.utc).isoformat(),
        "failure_reason": "abandoned",
    }).eq("user_id", user_id).eq("is_active", True).execute()


# ══════════════════════════════════════════════════════════════════════════════
# MODELOS DE REQUEST
# ══════════════════════════════════════════════════════════════════════════════

class AnswerRequest(BaseModel):
    question_id:     str
    selected_answer: str   # "IN" o "OUT"


# ══════════════════════════════════════════════════════════════════════════════
# 1. INICIAR INTENTO
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/attempts")
async def start_attempt(
    user: dict = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """
    Inicia un nuevo intento de Nerdocrasy.
    Cancela cualquier intento activo previo (abandoned).
    Devuelve el attempt_id y el perfil actual del ciudadano.
    """
    user_id = user["id"]

    # Obtener perfil actual
    profile_resp = (
        supabase.table("nerdocrasy_profiles")
        .select("best_level, best_stage, total_attempts")
        .eq("user_id", user_id)
        .execute()
    )
    profile = profile_resp.data[0] if profile_resp.data else {"best_level": 0, "best_stage": None, "total_attempts": 0}

    # Cancelar intentos activos previos
    _cancel_active_attempts(supabase, user_id)

    # Butaca (opcional — puede ser None si no verificado)
    seat_id = None
    try:
        si_resp = (
            supabase.table("seat_identities")
            .select("butaca_numero")
            .eq("user_id", user_id)
            .execute()
        )
        if si_resp.data:
            seat_id = si_resp.data[0]["butaca_numero"]
    except Exception:
        pass

    # Crear intento
    attempt_resp = supabase.table("nerdocrasy_attempts").insert({
        "user_id":              user_id,
        "seat_id":              seat_id,
        "starting_best_level":  profile["best_level"],
        "highest_level_completed": 0,
        "current_level":        1,
        "is_active":            True,
    }).execute()

    attempt = attempt_resp.data[0]

    logger.info(f"Nerdocrasy: nuevo intento {attempt['id']} para user {user_id[:8]}")

    return {
        "attempt_id":    attempt["id"],
        "current_level": 1,
        "stage":         "KNOW",
        "profile": {
            "best_level":    profile["best_level"],
            "best_stage":    profile["best_stage"],
            "total_attempts": profile["total_attempts"],
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# 2. OBTENER PREGUNTA DEL NIVEL ACTUAL
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/attempts/{attempt_id}/question")
async def get_question(
    attempt_id: str,
    user: dict = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """
    Devuelve la pregunta para el nivel actual del intento.
    Registra el deadline del servidor.
    NUNCA devuelve correct_answer.
    """
    user_id = user["id"]

    # Verificar que el intento pertenece al usuario y está activo
    att_resp = (
        supabase.table("nerdocrasy_attempts")
        .select("id, user_id, is_active, current_level, current_question_id, current_question_deadline")
        .eq("id", attempt_id)
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not att_resp.data:
        raise HTTPException(status_code=404, detail="Intento no encontrado")
    attempt = att_resp.data

    if not attempt["is_active"]:
        raise HTTPException(status_code=409, detail="El intento ya finalizó")

    current_level = attempt["current_level"]
    stage         = level_to_stage(current_level)

    # Si ya hay una pregunta activa con deadline vigente, devolvemos la misma
    if attempt["current_question_id"] and attempt["current_question_deadline"]:
        dl = datetime.fromisoformat(attempt["current_question_deadline"])
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) < dl:
            # Reusar la pregunta activa
            q_resp = (
                supabase.table("nerdocrasy_questions")
                .select("id, level, stage, question_type, question_text, "
                        "visual_asset_url, visual_asset_a_url, visual_asset_b_url, "
                        "visual_alt, visual_caption, visual_layout")
                .eq("id", attempt["current_question_id"])
                .single()
                .execute()
            )
            if q_resp.data:
                remaining_ms = int((dl - datetime.now(timezone.utc)).total_seconds() * 1000)
                return {
                    "question":     q_resp.data,
                    "level":        current_level,
                    "stage":        stage,
                    "deadline_utc": dl.isoformat(),
                    "remaining_ms": max(0, remaining_ms),
                }

    # Seleccionar nueva pregunta
    question = select_question(supabase, user_id, current_level)
    if not question:
        raise HTTPException(
            status_code=503,
            detail=f"No hay preguntas disponibles para el nivel {current_level}. Contacta al administrador."
        )

    # Registrar deadline en el intento
    dl = question_deadline()
    supabase.table("nerdocrasy_attempts").update({
        "current_question_id":       question["id"],
        "current_question_deadline": dl.isoformat(),
    }).eq("id", attempt_id).execute()

    logger.info(
        f"Nerdocrasy: pregunta {question['id'][:8]} nivel {current_level} "
        f"para intento {attempt_id[:8]}, deadline {dl.isoformat()}"
    )

    return {
        "question":     get_question_for_client(question),
        "level":        current_level,
        "stage":        stage,
        "deadline_utc": dl.isoformat(),
        "remaining_ms": 8000,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. ENVIAR RESPUESTA
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/attempts/{attempt_id}/answer")
async def submit_answer(
    attempt_id: str,
    body: AnswerRequest,
    user: dict = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    """
    Valida la respuesta del ciudadano.

    Checks:
      - Intento activo y pertenece al usuario
      - question_id es la pregunta activa del intento
      - No fue respondida ya
      - Deadline no expiró (server-side)
      - selected_answer es IN o OUT

    Retorna:
      - Si correcta: { correct: true, completedLevel, nextLevel, stage }
      - Si incorrecta/timeout: { correct: false, attemptFinished: true,
                                 correctAnswer, explanation, highestLevelCompleted,
                                 historicalBest, isNewRecord }
    """
    user_id = user["id"]

    if body.selected_answer not in ("IN", "OUT"):
        raise HTTPException(status_code=400, detail="selected_answer debe ser IN o OUT")

    # Obtener intento
    att_resp = (
        supabase.table("nerdocrasy_attempts")
        .select("*")
        .eq("id", attempt_id)
        .eq("user_id", user_id)
        .single()
        .execute()
    )
    if not att_resp.data:
        raise HTTPException(status_code=404, detail="Intento no encontrado")
    attempt = att_resp.data

    if not attempt["is_active"]:
        raise HTTPException(status_code=409, detail="El intento ya finalizó")

    if attempt["current_question_id"] != body.question_id:
        raise HTTPException(status_code=400, detail="question_id no corresponde al nivel actual")

    # Verificar que no fue respondida ya en este intento
    already_resp = (
        supabase.table("nerdocrasy_attempt_answers")
        .select("id")
        .eq("attempt_id", attempt_id)
        .eq("question_id", body.question_id)
        .execute()
    )
    if already_resp.data:
        raise HTTPException(status_code=409, detail="Esta pregunta ya fue respondida en este intento")

    # Verificar deadline SERVER-SIDE
    dl_str = attempt.get("current_question_deadline")
    dl = datetime.fromisoformat(dl_str) if dl_str else None
    within_time = is_within_deadline(dl)

    # Obtener la pregunta (con correct_answer — solo uso interno)
    q_resp = (
        supabase.table("nerdocrasy_questions")
        .select("id, level, question_text, correct_answer, explanation, visual_asset_url")
        .eq("id", body.question_id)
        .single()
        .execute()
    )
    if not q_resp.data:
        raise HTTPException(status_code=404, detail="Pregunta no encontrada")
    question = q_resp.data

    correct_answer = question["correct_answer"]
    now = datetime.now(timezone.utc)

    # Calcular tiempo de respuesta
    if dl:
        deadline_dt = dl if dl.tzinfo else dl.replace(tzinfo=timezone.utc)
        elapsed_ms = max(0, int((8 - (deadline_dt - now).total_seconds()) * 1000))
    else:
        elapsed_ms = 8000

    # Determinar si fue correcto (timeout cuenta como incorrecto)
    is_correct = within_time and (body.selected_answer == correct_answer)
    failure_reason = None if is_correct else ("timeout" if not within_time else "wrong_answer")

    current_level = attempt["current_level"]

    # Registrar respuesta con snapshot inmutable
    supabase.table("nerdocrasy_attempt_answers").insert({
        "attempt_id":              attempt_id,
        "question_id":             body.question_id,
        "level":                   current_level,
        "selected_answer":         body.selected_answer if within_time else "TIMEOUT",
        "correct_answer_snapshot": correct_answer,
        "is_correct":              is_correct,
        "response_time_ms":        elapsed_ms,
        "question_text_snapshot":  question["question_text"],
        "explanation_snapshot":    question["explanation"],
        "visual_asset_snapshot":   question.get("visual_asset_url"),
        "answered_at":             now.isoformat(),
    }).execute()

    # Registrar exposición
    record_exposure(supabase, user_id, body.question_id, current_level, is_correct)

    if is_correct:
        # ── Avanzar al siguiente nivel ──────────────────────────────────────
        new_highest = max(attempt["highest_level_completed"], current_level)
        next_level  = current_level + 1
        next_stage  = level_to_stage(next_level) if next_level <= 90 else "TRAP"

        if next_level > 90:
            # ¡Completó el nivel 90! Fin glorioso
            is_record = update_best_level(supabase, user_id, attempt.get("seat_id"), attempt_id, 90)
            total_ms  = (attempt.get("total_answer_time_ms") or 0) + elapsed_ms

            supabase.table("nerdocrasy_attempts").update({
                "is_active":               False,
                "finished_at":             now.isoformat(),
                "highest_level_completed": 90,
                "total_answer_time_ms":    total_ms,
                "is_personal_record":      is_record,
                "current_question_id":     None,
                "current_question_deadline": None,
            }).eq("id", attempt_id).execute()

            return {
                "correct":               True,
                "completedLevel":        90,
                "attemptFinished":       True,
                "nerdocrasiaComplete":   True,
                "isNewRecord":           is_record,
                "highestLevelCompleted": 90,
                "historicalBest":        90,
            }

        # Avanzar nivel
        total_ms = (attempt.get("total_answer_time_ms") or 0) + elapsed_ms
        is_stage_transition = level_to_stage(current_level) != next_stage

        supabase.table("nerdocrasy_attempts").update({
            "highest_level_completed": new_highest,
            "current_level":           next_level,
            "current_question_id":     None,
            "current_question_deadline": None,
            "total_answer_time_ms":    total_ms,
        }).eq("id", attempt_id).execute()

        logger.info(f"Nerdocrasy: intento {attempt_id[:8]} avanzó a nivel {next_level}")

        return {
            "correct":           True,
            "completedLevel":    current_level,
            "nextLevel":         next_level,
            "stage":             next_stage,
            "isStageTransition": is_stage_transition,
        }

    else:
        # ── Eliminado ───────────────────────────────────────────────────────
        highest = attempt["highest_level_completed"]
        total_ms = (attempt.get("total_answer_time_ms") or 0) + elapsed_ms

        # Actualizar best_level si aplica
        is_record = False
        if highest > 0:
            is_record = update_best_level(supabase, user_id, attempt.get("seat_id"), attempt_id, highest)

        supabase.table("nerdocrasy_attempts").update({
            "is_active":               False,
            "finished_at":             now.isoformat(),
            "failed_level":            current_level,
            "failure_reason":          failure_reason,
            "total_answer_time_ms":    total_ms,
            "is_personal_record":      is_record,
            "current_question_id":     None,
            "current_question_deadline": None,
        }).eq("id", attempt_id).execute()

        # Obtener best_level actualizado
        prof_resp = (
            supabase.table("nerdocrasy_profiles")
            .select("best_level")
            .eq("user_id", user_id)
            .execute()
        )
        historical_best = prof_resp.data[0]["best_level"] if prof_resp.data else 0

        logger.info(
            f"Nerdocrasy: intento {attempt_id[:8]} terminó en nivel {current_level} "
            f"({failure_reason}), highest={highest}, record={is_record}"
        )

        return {
            "correct":               False,
            "attemptFinished":       True,
            "failureReason":         failure_reason,
            "correctAnswer":         correct_answer,
            "explanation":           question["explanation"],
            "highestLevelCompleted": highest,
            "historicalBest":        historical_best,
            "isNewRecord":           is_record,
        }


# ══════════════════════════════════════════════════════════════════════════════
# 4. PERFIL DEL CIUDADANO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/profile")
async def get_profile(
    user: dict = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    user_id = user["id"]

    prof_resp = (
        supabase.table("nerdocrasy_profiles")
        .select("best_level, best_stage, best_achieved_at, total_attempts")
        .eq("user_id", user_id)
        .execute()
    )

    if not prof_resp.data:
        return {"best_level": 0, "best_stage": None, "total_attempts": 0, "global_rank": None}

    profile = prof_resp.data[0]

    # Calcular rank actual
    rank = None
    try:
        rank_resp = supabase.rpc("get_nerdocrasy_my_rank", {"p_user_id": user_id}).execute()
        rank = rank_resp.data
    except Exception:
        pass

    return {
        "best_level":       profile["best_level"],
        "best_stage":       profile["best_stage"],
        "best_achieved_at": profile.get("best_achieved_at"),
        "total_attempts":   profile["total_attempts"],
        "global_rank":      rank,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. RANKING GLOBAL
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/ranking")
async def get_ranking(
    limit: int = 50,
    user: dict = Depends(_verificar_usuario),
    supabase: Client = Depends(get_supabase),
):
    if limit > 100:
        limit = 100

    rank_resp = supabase.rpc("get_nerdocrasy_ranking", {"limit_n": limit}).execute()
    rows = rank_resp.data or []

    # Enriquecer con alias/butaca desde profiles (no expone user_id al cliente)
    result = []
    for row in rows:
        result.append({
            "rank":            row["rank_position"],
            "seat_id":         row["seat_id"],
            "best_level":      row["best_level"],
            "best_stage":      row["best_stage"],
            "best_achieved_at": row["best_achieved_at"],
            "total_attempts":  row["total_attempts"],
        })

    return {"ranking": result, "total": len(result)}
