"""
Nerdocrasy — Game Engine Service

Responsabilidades:
  - Selección de preguntas server-side (prioridad: no vistas → menos vistas → random)
  - Validación de timer server-side (8 segundos)
  - Cálculo de stage a partir del nivel
  - Determinación de personal record
"""
import random
from datetime import datetime, timezone, timedelta
from typing import Optional

from supabase import Client

QUESTION_TIME_SECONDS = 8
TOTAL_LEVELS = 90
STAGE_KNOW  = (1,  30)
STAGE_THINK = (31, 60)
STAGE_TRAP  = (61, 90)


def level_to_stage(level: int) -> str:
    if level <= 30:
        return "KNOW"
    elif level <= 60:
        return "THINK"
    return "TRAP"


def question_deadline() -> datetime:
    """Calcula el deadline del servidor para la pregunta actual."""
    return datetime.now(timezone.utc) + timedelta(seconds=QUESTION_TIME_SECONDS)


def is_within_deadline(deadline: Optional[datetime]) -> bool:
    """True si la respuesta llegó antes del deadline."""
    if deadline is None:
        return False
    now = datetime.now(timezone.utc)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return now <= deadline


def select_question(
    supabase: Client,
    user_id: str,
    level: int,
) -> Optional[dict]:
    """
    Selecciona UNA pregunta para el nivel dado.

    Prioridad:
      1. Preguntas que el ciudadano NUNCA ha visto en este nivel.
      2. Preguntas vistas hace más tiempo (last_seen_at más antigua).
      3. Random entre candidatos equivalentes.

    NUNCA devuelve correct_answer — eso solo lo usa el engine en el momento de validar.
    """
    # Obtener todas las preguntas activas para el nivel
    qs_resp = (
        supabase.table("nerdocrasy_questions")
        .select(
            "id, level, stage, question_type, question_text, "
            "visual_asset_url, visual_asset_a_url, visual_asset_b_url, "
            "visual_alt, visual_caption, visual_layout, correct_answer"  # correct_answer solo para uso interno
        )
        .eq("level", level)
        .eq("active", True)
        .execute()
    )
    questions = qs_resp.data or []
    if not questions:
        return None

    # Obtener historial de exposición del ciudadano para este nivel
    exp_resp = (
        supabase.table("nerdocrasy_question_exposure")
        .select("question_id, last_seen_at, times_seen")
        .eq("user_id", user_id)
        .eq("level", level)
        .execute()
    )
    exposure = {e["question_id"]: e for e in (exp_resp.data or [])}

    # Separar en nunca vistas vs vistas
    never_seen = [q for q in questions if q["id"] not in exposure]
    seen       = [q for q in questions if q["id"] in exposure]

    if never_seen:
        # PRIORIDAD 1: nunca vistas, elegir al azar entre ellas
        chosen = random.choice(never_seen)
    elif seen:
        # PRIORIDAD 2: la vista hace más tiempo
        seen.sort(key=lambda q: exposure[q["id"]]["last_seen_at"])
        # Tomar las que tienen el mismo last_seen_at más antiguo y elegir al azar
        oldest_ts = exposure[seen[0]["id"]]["last_seen_at"]
        oldest_group = [q for q in seen if exposure[q["id"]]["last_seen_at"] == oldest_ts]
        chosen = random.choice(oldest_group)
    else:
        return None

    return chosen


def get_question_for_client(question: dict) -> dict:
    """
    Filtra los campos de una pregunta para enviar al cliente.
    NUNCA incluye correct_answer.
    """
    return {
        "id":                question["id"],
        "level":             question["level"],
        "stage":             question["stage"],
        "question_type":     question["question_type"],
        "question_text":     question["question_text"],
        "visual_asset_url":  question.get("visual_asset_url"),
        "visual_asset_a_url": question.get("visual_asset_a_url"),
        "visual_asset_b_url": question.get("visual_asset_b_url"),
        "visual_alt":        question.get("visual_alt"),
        "visual_caption":    question.get("visual_caption"),
        "visual_layout":     question.get("visual_layout", "SINGLE"),
    }


def record_exposure(
    supabase: Client,
    user_id: str,
    question_id: str,
    level: int,
    is_correct: bool,
) -> None:
    """Registra/actualiza la exposición del ciudadano a esta pregunta."""
    now_iso = datetime.now(timezone.utc).isoformat()

    existing = (
        supabase.table("nerdocrasy_question_exposure")
        .select("times_seen, times_correct, times_wrong")
        .eq("user_id", user_id)
        .eq("question_id", question_id)
        .execute()
    )

    if existing.data:
        row = existing.data[0]
        supabase.table("nerdocrasy_question_exposure").update({
            "last_seen_at": now_iso,
            "times_seen":    row["times_seen"] + 1,
            "times_correct": row["times_correct"] + (1 if is_correct else 0),
            "times_wrong":   row["times_wrong"]   + (0 if is_correct else 1),
        }).eq("user_id", user_id).eq("question_id", question_id).execute()
    else:
        supabase.table("nerdocrasy_question_exposure").insert({
            "user_id":      user_id,
            "question_id":  question_id,
            "level":        level,
            "first_seen_at": now_iso,
            "last_seen_at": now_iso,
            "times_seen":   1,
            "times_correct": 1 if is_correct else 0,
            "times_wrong":   0 if is_correct else 1,
        }).execute()


def update_best_level(
    supabase: Client,
    user_id: str,
    seat_id: Optional[int],
    attempt_id: str,
    completed_level: int,
) -> bool:
    """
    Actualiza best_level en nerdocrasy_profiles si el nivel completado es mayor.
    Retorna True si fue un nuevo récord personal.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    stage = level_to_stage(completed_level)

    existing = (
        supabase.table("nerdocrasy_profiles")
        .select("best_level, total_attempts")
        .eq("user_id", user_id)
        .execute()
    )

    if existing.data:
        current_best = existing.data[0]["best_level"]
        total_att    = existing.data[0]["total_attempts"]
        is_record    = completed_level > current_best

        update_data: dict = {"total_attempts": total_att + 1, "updated_at": now_iso}
        if is_record:
            update_data.update({
                "best_level":       completed_level,
                "best_stage":       stage,
                "best_attempt_id":  attempt_id,
                "best_achieved_at": now_iso,
            })

        supabase.table("nerdocrasy_profiles").update(update_data).eq("user_id", user_id).execute()
    else:
        # Primer intento
        is_record = completed_level > 0
        supabase.table("nerdocrasy_profiles").insert({
            "user_id":          user_id,
            "seat_id":          seat_id,
            "best_level":       completed_level if is_record else 0,
            "best_stage":       stage if is_record else None,
            "best_attempt_id":  attempt_id if is_record else None,
            "best_achieved_at": now_iso if is_record else None,
            "total_attempts":   1,
            "updated_at":       now_iso,
        }).execute()

    return is_record
