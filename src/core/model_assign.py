"""Model assignment — elige el LLM de una gema/ayudante segun lo disponible en la PC.

Portable: NO referencia ids de modelo concretos. Selecciona por CAPACIDAD
(code/reasoning/vision/fast/general) derivada del nombre de cada modelo instalado,
con overrides opcionales por config (assignment.overrides.<gema>).
"""
from __future__ import annotations

from typing import Any

_CAP_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("embed", ("embed",)),
    ("vision", ("vl-", "vl:", "vision", "janus", "llava", "qwen3-vl", "qwen2.5vl")),
    ("code", ("coder", "code", "omnicoder", "codestral")),
    ("reasoning", ("r1", "reason", "think", "deepseek-r1")),
    ("fast", ("nano", "0.5b", "1.5b", "3b", "mini", "flash", "lightning", "tiny")),
)

# Especialidad de cada gema -> capacidad requerida. Es una propiedad de la gema,
# NO de la PC: no nombra modelos.
_GEMA_CAPABILITY: dict[str, str] = {
    "vision": "vision",
    "code": "code", "codex": "code", "engineer": "code", "tester": "code",
    "optimizer": "code", "opencode": "code", "devops": "code", "debugger": "code",
    "analyst": "reasoning", "security": "reasoning", "scholar": "reasoning",
    "architect": "reasoning", "sage": "reasoning", "biblioteca": "reasoning", "verifier": "reasoning",
    "director": "general", "ayuda": "general", "creative": "general",
    "design": "general", "music": "general", "trainer": "general",
    "producer": "general", "prompter": "general",
}

_HINT_BY_MODEL_HINT: dict[str, str | None] = {"quality": "coding", "speed": "simple", "balanced": None}


def capabilities_of(model_id: str) -> set[str]:
    mid = (model_id or "").lower()
    caps: set[str] = set()
    for cap, needles in _CAP_RULES:
        if any(n in mid for n in needles):
            caps.add(cap)
    if not caps:
        caps.add("general")
    return caps


def capability_for_gema(gema: str, model_hint: str = "") -> str:
    if gema in _GEMA_CAPABILITY:
        return _GEMA_CAPABILITY[gema]
    mh = (model_hint or "").strip().lower()
    if mh == "speed":
        return "fast"
    if mh == "quality":
        return "reasoning"
    return "general"


def _hint_for(gema: str, model_hint: str) -> str | None:
    try:
        from .gema_profiles import hint_for_gema
        h = hint_for_gema(gema)
        if h:
            return h
    except Exception:
        pass
    return _HINT_BY_MODEL_HINT.get((model_hint or "").strip().lower())


def _best_for_capability(available: dict[str, Any], cap: str) -> tuple[str, Any] | None:
    cands = [(mid, m) for mid, m in available.items()
             if cap in capabilities_of(mid) and "embed" not in capabilities_of(mid)]
    if not cands:
        return None
    return max(cands, key=lambda x: x[1].effective_tier())


def assign_model(
    router: Any,
    *,
    gema: str = "",
    model_hint: str = "",
    explicit: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resuelve {model, provider, reason} para una gema/ayudante según lo instalado.

    Prioridad: explícito > override de config > capacidad (mejor instalado) > hint del router > primer chat.
    """
    available: dict[str, Any] = {}
    try:
        available = {m.id: m for m in router.list_models()}
    except Exception:
        available = {}

    def _res(model: str, provider: str, reason: str) -> dict[str, Any]:
        return {"model": model, "provider": provider, "reason": reason, "catalog_size": len(available)}

    if explicit:
        prov = available[explicit].provider_name if explicit in available else ""
        return _res(explicit, prov, "modelo explícito del ayudante")

    overrides = overrides or {}
    ov = overrides.get(gema) if gema else None
    if ov and ov in available:
        return _res(ov, available[ov].provider_name, f"override de config para '{gema}'")

    cap = capability_for_gema(gema, model_hint)
    if cap != "general":
        best = _best_for_capability(available, cap)
        if best is not None:
            return _res(best[0], best[1].provider_name, f"capacidad '{cap}' (mejor instalado)")

    hint = _hint_for(gema, model_hint)
    try:
        routed = router.resolve(task_hint=hint) if hint else router.resolve()
        if routed is not None:
            return _res(routed.model, routed.route.provider_name, f"router hint={hint or 'none'}")
    except Exception:
        pass

    for mid, m in available.items():
        if "embed" not in capabilities_of(mid):
            return _res(mid, m.provider_name, "primer modelo de chat disponible")
    return _res("", "", "sin modelos disponibles")
