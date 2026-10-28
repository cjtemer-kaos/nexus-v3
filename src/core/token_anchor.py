"""Ancla la estimacion de tokens en el usage REAL que reporta el proveedor.

`chars/4` no alcanza para decidir compactacion ni poda: medido el 2026-10-28 en
la sesion 445b758f71fd, el request se estimo en 13.800 tokens y Ollama conto
16.214 (17 % mas). La causa es que el JSON de los schemas, el codigo y el texto
en espanol son mas densos que 4 caracteres por token.

El harness de DeepSeek resuelve lo mismo con `token-meter`: ancla en el `usage`
de la ultima llamada y solo cotiza el delta. Aca se hace igual, con caracteres
en vez de un tokenizador:

    record(session_id, chars, tokens)   # despues de cada llamada real
    estimate(session_id, chars)         # antes de cada llamada

`prompt_eval_count` de Ollama es el tamano COMPLETO del prompt (verificado: dos
requests identicas devuelven el mismo numero, 3016 y 3016), asi que sirve de
ancla aunque el prefijo venga de cache.

El estado vive en memoria por sesion: `AgentLoop` se instancia por request y el
ancla tiene que sobrevivir entre las llamadas del mismo turno.
"""
from __future__ import annotations

DEFAULT_TOKENS_PER_CHAR = 0.25
_TOKENS_PER_CHAR_MIN = 0.05
_TOKENS_PER_CHAR_MAX = 1.5
_MAX_SESIONES = 512

# session_id -> (chars del request, tokens que conto el proveedor)
_ANCLA: dict[str, tuple[int, int]] = {}


def record(session_id: str, chars: int, tokens: int) -> None:
    """Guarda el request recien enviado y lo que el proveedor conto de verdad."""
    try:
        chars = int(chars or 0)
        tokens = int(tokens or 0)
        if not session_id or chars <= 0 or tokens <= 0:
            return
        if len(_ANCLA) >= _MAX_SESIONES and session_id not in _ANCLA:
            _ANCLA.pop(next(iter(_ANCLA)), None)
        _ANCLA[session_id] = (chars, tokens)
    except Exception:
        return


def has_anchor(session_id: str) -> bool:
    return bool(session_id) and session_id in _ANCLA


def tokens_per_char(session_id: str, default: float = DEFAULT_TOKENS_PER_CHAR) -> float:
    """Densidad observada (tokens por caracter) en la ultima llamada real."""
    ancla = _ANCLA.get(session_id)
    if not ancla:
        return default
    chars, tokens = ancla
    if chars <= 0:
        return default
    densidad = float(tokens) / float(chars)
    return max(_TOKENS_PER_CHAR_MIN, min(_TOKENS_PER_CHAR_MAX, densidad))


def estimate(session_id: str, chars: int, fallback: int = 0) -> int:
    """Tokens estimados para un request de `chars` caracteres.

    Con ancla escala el numero real por el cociente de caracteres, asi la poda
    (que baja caracteres) baja tokens en la misma proporcion. Sin ancla devuelve
    `fallback`, que es lo que el llamador ya calculaba con chars/4.
    """
    try:
        chars = int(chars or 0)
    except Exception:
        return int(fallback or 0)
    ancla = _ANCLA.get(session_id)
    if not ancla:
        return int(fallback or 0)
    if chars <= 0:
        return 0
    anchor_chars, anchor_tokens = ancla
    return max(1, int(round(float(anchor_tokens) * float(chars) / float(anchor_chars))))


def tokens_for(session_id: str, chars: int, fallback: int = 0) -> int:
    """Tokens de UNA parte del request (schemas, system) a la densidad observada."""
    try:
        chars = int(chars or 0)
    except Exception:
        return int(fallback or 0)
    if chars <= 0:
        return int(fallback or 0)
    if not has_anchor(session_id):
        return int(fallback or 0)
    return max(1, int(round(chars * tokens_per_char(session_id))))


def reset(session_id: str = "") -> None:
    """Olvida el ancla (una sesion, o todas si no se pasa id). Para tests."""
    if session_id:
        _ANCLA.pop(session_id, None)
    else:
        _ANCLA.clear()
