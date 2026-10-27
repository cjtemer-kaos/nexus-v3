"""Error Classifier — RETRY / SKIP / REPLAN / ABORT.

Clasifica el fallo de una llamada LLM o tool en RETRY / SKIP / REPLAN / ABORT.
"""
from __future__ import annotations

import re
from enum import Enum


class ErrorAction(str, Enum):
    RETRY = "retry"
    SKIP = "skip"
    REPLAN = "replan"
    ABORT = "abort"


# Patterns (ordered by priority: transient -> fatal -> replan -> skip)
_TRANSIENT_PATTERNS = [
    r"timeout", r"timed out", r"deadline", r"rate limit", r"429", r"503", r"502", r"504",
    r"connection", r"reset", r"econn", r"temporarily unavailable", r"overloaded",
    r"too many requests", r"service unavailable", r"try again",
]

_FATAL_PATTERNS = [
    r"permission denied", r"not found", r"no such file", r"invalid state",
    r"scope denied", r"forbidden", r"unauthorized", r"authentication", r"api key",
    r"quota exceeded", r"billing", r"invalid api",
]

_SKIP_PATTERNS = [
    r"not critical", r"optional", r"skip", r"already exists", r"duplicate",
]

# Denegaciones de alcance/seguridad (scope.py en español + hook block + read_only).
# Sin esto caen al default REPLAN y el agente reintenta lo que deberia abortar/omitir.
_SCOPE_DENY_PATTERNS = [
    r"scope denied", r"permission denied", r"denegad", r"bloquead", r"hook bloqu",
    r"read_only", r"read-only", r"fuera de (alcance|ra[ií]ces)", r"no permitid", r"no autorizad",
]

# Replan: bad params, validation, schema mismatch — retry with different args
_REPLAN_PATTERNS = [
    r"invalid (param|argument|value|tool)", r"missing (param|field|required)",
    r"validation", r"schema", r"unexpected", r"unknown tool", r"not found.*tool",
    r"json.*error", r"parse.*error", r"bad request", r"400",
]


def _matches(text: str, patterns: list[str]) -> bool:
    lower = text.lower()
    return any(re.search(p, lower) for p in patterns)


def classify_error(tool: str, err: str, *, context: dict | None = None) -> ErrorAction:
    """Classify an error string into an ErrorAction.

    Priority: FATAL (ABORT) > TRANSIENT (RETRY) > SKIP > REPLAN (default for tool errors)
    """
    if not err:
        return ErrorAction.RETRY
    # Denegacion de alcance/seguridad: ABORT si es de red/permiso, si no SKIP.
    if _matches(err, _SCOPE_DENY_PATTERNS):
        _low = err.lower()
        # "red" como subcadena matchea 'rendered'/'redacted': exigir frase de red.
        _is_net = (" de red" in _low) or ("permitir_red" in _low) or ("network" in _low)
        return ErrorAction.ABORT if (_is_net or "permission" in _low) else ErrorAction.SKIP
    if _matches(err, _FATAL_PATTERNS):
        # Some fatals are SKIP if tool is optional
        if context and context.get("optional"):
            return ErrorAction.SKIP
        return ErrorAction.ABORT
    if _matches(err, _TRANSIENT_PATTERNS):
        return ErrorAction.RETRY
    if _matches(err, _SKIP_PATTERNS):
        return ErrorAction.SKIP
    if _matches(err, _REPLAN_PATTERNS):
        return ErrorAction.REPLAN
    # Default: replan (bad args) for tool errors, retry for unknown infrastructure
    # Heuristic: if tool name appears in error, likely param error -> replan
    if tool.lower() in err.lower():
        return ErrorAction.REPLAN
    return ErrorAction.REPLAN


def should_retry(action: ErrorAction, attempt: int, max_retries: int) -> bool:
    return action == ErrorAction.RETRY and attempt < max_retries


def should_replan(action: ErrorAction, attempt: int, max_replans: int) -> bool:
    return action == ErrorAction.REPLAN and attempt < max_replans
