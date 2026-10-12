"""Estimación de costo por sesión (heurística; local = gratis).

Los mensajes guardan el `model`, no el proveedor. Clasificamos por heurística:
modelos locales (Ollama/llama.cpp, ids con ':' o familias conocidas) = 0; el resto = tarifa cloud.
Las tarifas son configurables vía `cost.rates` en config.yaml.
"""
from __future__ import annotations

_DEFAULT_RATES = {"local": 0.0, "cloud": 0.002}  # USD por 1K tokens

_LOCAL_HINTS = ("qwen", "llama", "gemma", "deepseek-r1", "nemotron", "phi", "mistral",
                "codellama", "nomic-embed", "janus", "moondream", "bakllava", "vl:")


def provider_of(model: str, rates: dict | None = None) -> str:
    m = (model or "").lower()
    if ":" in m or any(h in m for h in _LOCAL_HINTS):
        return "local"
    return "cloud"


def rates(cfg: dict | None = None) -> dict:
    try:
        r = (cfg or {}).get("cost", {}).get("rates")
        if isinstance(r, dict):
            return {**_DEFAULT_RATES, **{k: float(v) for k, v in r.items()}}
    except Exception:
        pass
    return dict(_DEFAULT_RATES)


def estimate_cost(tokens: int, model: str, cfg: dict | None = None) -> float:
    r = rates(cfg)
    prov = provider_of(model, r)
    return round((tokens or 0) / 1000.0 * float(r.get(prov, 0.0)), 4)


def cost_by_model(by_model: list[dict], cfg: dict | None = None) -> dict:
    """Devuelve {model: costo} + total, a partir de la salida de session_log.usage()."""
    per = {}
    total = 0.0
    for m in by_model or []:
        c = estimate_cost(int(m.get("tokens", 0)), str(m.get("model", "")), cfg)
        per[str(m.get("model", "?"))] = c
        total += c
    return {"per_model": per, "total_usd": round(total, 4)}
