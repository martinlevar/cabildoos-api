"""
Servicio Gemini para el Playroom — Nerdmocracy y Yo, Presidente.
Reutiliza el cliente ya inicializado en services/gemini.py.
"""
import asyncio
import logging

import services.gemini as _gemini_svc
from services.gemini import _extract_json

logger = logging.getLogger(__name__)


# ── Capa de llamada de texto puro (sin imagen) ─────────────────────────────

def _call_text(prompt: str) -> str:
    """Llamada sincrónica a Gemini con solo texto — ejecutar en thread separado."""
    client = _gemini_svc._gemini_client
    if client is None:
        raise RuntimeError("Gemini client no inicializado — init_gemini() no fue llamado")

    response = client.models.generate_content(
        model=_gemini_svc._GEMINI_MODEL,
        contents=prompt,
    )

    usage = getattr(response, "usage_metadata", None)
    tokens_in  = getattr(usage, "prompt_token_count", 0) or 0
    tokens_out = getattr(usage, "candidates_token_count", 0) or 0
    _gemini_svc._record_call(tokens_in, tokens_out)

    return response.text


# ══════════════════════════════════════════════════════════════════════════════
# NERDMOCRACY
# ══════════════════════════════════════════════════════════════════════════════

async def generar_pregunta_nerdmocracy() -> dict:
    """
    Genera una pregunta de trivia cívica venezolana vía Gemini.

    Retorna:
        question_text  str
        options        list[str]  (4 opciones)
        correct_index  int        (0-3)  ← NUNCA enviar al cliente
    """
    prompt = (
        'Eres el generador de preguntas de "Nerdmocracy", trivia cívica venezolana.\n\n'
        "Genera UNA pregunta de trivia sobre uno de estos temas:\n"
        "- Historia de Venezuela (personajes, fechas clave, eventos)\n"
        "- Democracia y política venezolana\n"
        "- Historia latinoamericana relevante\n"
        "- Derechos humanos y civismo\n"
        "- Constitución venezolana\n"
        "- Cultura venezolana general\n\n"
        "REGLAS CRÍTICAS:\n"
        "1. La pregunta debe leerse y entenderse en MENOS DE 5 SEGUNDOS — sé conciso\n"
        "2. Máximo 110 caracteres en la pregunta\n"
        "3. Factual y verificable (no de opinión)\n"
        "4. Las 4 opciones deben ser plausibles pero solo UNA correcta\n"
        "5. Cada opción: máximo 35 caracteres\n\n"
        "Responde SOLO con JSON válido, sin texto adicional:\n"
        '{"question_text":"¿Pregunta?","options":["A","B","C","D"],"correct_index":0}\n\n'
        "correct_index es el índice (0-3) de la opción correcta."
    )

    try:
        text = await asyncio.to_thread(_call_text, prompt)
        data = _extract_json(text)
    except Exception as exc:
        _gemini_svc._record_error(str(exc))
        raise

    if not all(k in data for k in ("question_text", "options", "correct_index")):
        raise ValueError(f"Respuesta Gemini incompleta: {data}")
    if len(data["options"]) != 4:
        raise ValueError(f"Se esperaban 4 opciones, llegaron {len(data['options'])}")
    ci = int(data["correct_index"])
    if ci not in (0, 1, 2, 3):
        raise ValueError(f"correct_index fuera de rango: {ci}")

    return {
        "question_text": str(data["question_text"])[:160],
        "options":       [str(o)[:60] for o in data["options"][:4]],
        "correct_index": ci,   # solo para almacenar server-side — nunca retornar al cliente
    }


# ══════════════════════════════════════════════════════════════════════════════
# YO, PRESIDENTE
# ══════════════════════════════════════════════════════════════════════════════

async def generar_escenario_yopresidente(dia: int, historia: list) -> dict:
    """
    Genera un escenario de crisis para el día `dia` del mandato.

    Retorna:
        crisis_text  str
        options      list[{text: str, risk_level: str}]  (3 opciones)
    """
    historia_str = ""
    if historia:
        recientes = historia[-5:]
        historia_str = "\nDecisiones anteriores:\n" + "\n".join(
            f"  Día {h['day']}: {h['decision']}" for h in recientes
        )

    prompt = (
        'Eres el narrador de "Yo, Presidente", juego de rol donde el usuario gobierna Venezuela.\n\n'
        f"Es el Día {dia} de tu mandato.{historia_str if historia_str else ' Es tu primer día.'}\n\n"
        "Genera UNA crisis o situación de gobierno que el presidente debe resolver HOY.\n"
        "Bases realistas: economía, energía, migración, seguridad, salud, inflación, servicios públicos.\n\n"
        "Genera exactamente 3 opciones con diferentes niveles de riesgo:\n"
        "  - Opción conservadora  (riesgo bajo,  impacto moderado)\n"
        "  - Opción moderada      (riesgo y beneficio balanceados)\n"
        "  - Opción audaz         (alto riesgo,  alto potencial)\n\n"
        "Responde SOLO con JSON válido:\n"
        '{"crisis_text":"Descripción ≤280 chars, tono dramático-informativo",'
        '"options":['
        '{"text":"Opción 1 ≤110 chars","risk_level":"low"},'
        '{"text":"Opción 2 ≤110 chars","risk_level":"medium"},'
        '{"text":"Opción 3 ≤110 chars","risk_level":"high"}'
        "]}"
    )

    try:
        text = await asyncio.to_thread(_call_text, prompt)
        data = _extract_json(text)
    except Exception as exc:
        _gemini_svc._record_error(str(exc))
        raise

    if "crisis_text" not in data or "options" not in data:
        raise ValueError(f"Respuesta Gemini incompleta: {data}")
    if len(data["options"]) != 3:
        raise ValueError(f"Se esperaban 3 opciones, llegaron {len(data['options'])}")

    return {
        "crisis_text": str(data["crisis_text"])[:380],
        "options": [
            {
                "text":       str(o.get("text", ""))[:140],
                "risk_level": str(o.get("risk_level", "medium")),
            }
            for o in data["options"][:3]
        ],
    }


async def generar_consecuencia_yopresidente(
    dia: int,
    crisis_text: str,
    decision_text: str,
    energia: int,
    capital_politico: int,
    salud_mental: int,
) -> dict:
    """
    Genera las consecuencias narrativas de una decisión presidencial.

    Retorna:
        consequence_text  str
        delta_energia     int  (-25 .. +20)
        delta_capital     int  (-25 .. +20)
        delta_salud       int  (-25 .. +20)
    """
    prompt = (
        'Eres el narrador de "Yo, Presidente", juego de rol donde el usuario gobierna Venezuela.\n\n'
        f"Día {dia}. La crisis fue:\n\"{crisis_text}\"\n\n"
        f"El presidente decidió:\n\"{decision_text}\"\n\n"
        "Estado actual de los medidores (0-100):\n"
        f"  Energía del pueblo:        {energia}/100\n"
        f"  Capital Político:          {capital_politico}/100\n"
        f"  Salud Mental (presidente): {salud_mental}/100\n\n"
        "Genera las consecuencias narrativas y los cambios en los medidores.\n\n"
        "REGLAS DE BALANCE:\n"
        "  - Cambios entre -25 y +20 por medidor\n"
        "  - Toda buena decisión implica trade-offs reales (no mejora todo igualmente)\n"
        "  - Una decisión mala empeora la mayoría\n"
        "  - Nunca hagas que los tres medidores mejoren exactamente igual\n"
        "  - Sé dramático pero justo\n\n"
        "Responde SOLO con JSON válido:\n"
        '{"consequence_text":"Narrativa ≤280 chars, tono dramático",'
        '"delta_energia":0,"delta_capital":-5,"delta_salud":5}'
    )

    try:
        text = await asyncio.to_thread(_call_text, prompt)
        data = _extract_json(text)
    except Exception as exc:
        _gemini_svc._record_error(str(exc))
        raise

    required = ("consequence_text", "delta_energia", "delta_capital", "delta_salud")
    if not all(k in data for k in required):
        raise ValueError(f"Respuesta Gemini incompleta: {data}")

    def clamp(v: int, lo: int = -25, hi: int = 20) -> int:
        return max(lo, min(hi, int(v)))

    return {
        "consequence_text": str(data["consequence_text"])[:380],
        "delta_energia":    clamp(data["delta_energia"]),
        "delta_capital":    clamp(data["delta_capital"]),
        "delta_salud":      clamp(data["delta_salud"]),
    }
