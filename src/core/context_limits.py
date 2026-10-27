"""Ventana de contexto REAL por modelo.

Cada modelo tiene su propio limite y Nexus tiene que saberlo para dos cosas:

1. No fallar. Ollama rechaza con 400 "exceeds the available context size" cuando el
   prompt supera el `num_ctx` con el que se cargo el modelo. Si mandamos un `num_ctx`
   mayor al que el modelo soporta, ese 400 aparece en el primer turno largo y el
   router lo confunde con "modelo roto" (turnos de 100-165 s medidos el 2026-10-26).
2. Compactar cuando corresponde. Si el presupuesto asume 16k y el modelo tiene 8k,
   compactamos tarde (overflow). Si el modelo tiene 128k, compactamos de gusto y
   perdemos contexto util.

De donde sale el dato, en orden:
  - Ollama  -> `/api/show` -> `model_info["<arquitectura>.context_length"]`
  - llama.cpp -> `/props`  -> `default_generation_settings.n_ctx`
  - catalogo del proveedor (ModelRoute.context_window) para cloud/BYOK
  - tabla KNOWN_CONTEXT como ultimo recurso local
  - DEFAULT_CONTEXT (lo que Ollama asume si nadie pasa `num_ctx`)
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("nexus-context")

# Ollama usa 8192 cuando el Modelfile no fija num_ctx: es el default seguro.
DEFAULT_CONTEXT = 8192
# Cloud (opencode-go, groq): ventanas grandes; 32k es conservador y no rompe nada.
DEFAULT_CLOUD_CONTEXT = 32768

# Fallback cuando no se puede preguntar al proveedor. Solo modelos que corren local.
KNOWN_CONTEXT: dict[str, int] = {
    "nomic-embed-text": 2048,
    "qwen2.5-coder:7b": 32768,
    "qwen2.5-coder": 32768,
    "qwen2.5vl:7b": 32768,
    "qwen2.5vl": 32768,
    "qwen3-vl-8b": 262144,
    "qwen3-vl-4b": 262144,
    "qwen3.5-9b": 262144,
    "qwen3:8b": 40960,
    "gemma4": 131072,
    "deepseek-r1:8b": 131072,
    "janus-pro-7b": 8192,
    "nemotron-3-nano:4b": 131072,
    "llama3.1:8b": 131072,
    "llama3.2:3b": 131072,
}

# Cache en proceso: model_id -> ventana nativa. Se llena con lo que responde el
# proveedor, que es la unica fuente confiable (los Modelfiles se re-crean).
_CACHE: dict[str, int] = {}


def normalize(model_id: str) -> str:
    """`qwen3-vl-8b:latest` y `qwen3-vl-8b` son el mismo modelo."""
    m = (model_id or "").strip()
    if m.endswith(":latest"):
        m = m[: -len(":latest")]
    return m


def register(model_id: str, context_length: int | None) -> None:
    """Guarda la ventana real que reporto el proveedor."""
    try:
        n = int(context_length or 0)
    except (TypeError, ValueError):
        return
    if n > 0:
        _CACHE[normalize(model_id)] = n


def known(model_id: str) -> int | None:
    """Ventana ya conocida (probe previo o tabla), o None si no sabemos."""
    m = normalize(model_id)
    if m in _CACHE:
        return _CACHE[m]
    if m in KNOWN_CONTEXT:
        return KNOWN_CONTEXT[m]
    base = m.split("/")[-1]
    if base in KNOWN_CONTEXT:
        return KNOWN_CONTEXT[base]
    return None


def cached(model_id: str) -> int | None:
    """Solo lo que reporto el proveedor (no la tabla): sirve para no re-sondear."""
    return _CACHE.get(normalize(model_id))


async def probe_ollama(base_url: str, model: str, timeout_s: float = 10.0) -> int | None:
    """Pregunta a Ollama el context_length nativo del modelo (`/api/show`)."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout_s) as cli:
            r = await cli.post(f"{base_url.rstrip('/')}/api/show", json={"model": model})
            r.raise_for_status()
            data = r.json() or {}
    except Exception as e:
        logger.debug("context: /api/show fallo para %s (%s)", model, e)
        return None
    info = data.get("model_info") or {}
    best: int | None = None
    for key, val in info.items():
        if not isinstance(key, str) or not key.endswith("context_length"):
            continue
        try:
            n = int(val)
        except (TypeError, ValueError):
            continue
        # Un modelo puede reportar varios (vision tower, text). El mayor es el usable.
        if n > 0 and (best is None or n > best):
            best = n
    register(model, best)
    return best


async def probe_llamacpp(base_url: str, timeout_s: float = 5.0) -> int | None:
    """llama.cpp: n_ctx real del server (`/props`)."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=timeout_s) as cli:
            r = await cli.get(f"{base_url.rstrip('/')}/props")
            r.raise_for_status()
            data = r.json() or {}
    except Exception as e:
        logger.debug("context: /props fallo (%s)", e)
        return None
    for key in ("default_generation_settings", "generation_settings"):
        sub = data.get(key) or {}
        for field in ("n_ctx", "n_batch"):
            try:
                n = int(sub.get(field) or 0)
            except (TypeError, ValueError):
                n = 0
            if n > 0:
                return n
    return None


def config_num_ctx(cfg: dict | None) -> int:
    """`providers.ollama.options.num_ctx` de config (0 si no esta fijado)."""
    try:
        opts = (((cfg or {}).get("providers") or {}).get("ollama") or {}).get("options") or {}
        return int(opts.get("num_ctx") or 0)
    except (TypeError, ValueError):
        return 0


def usable_window(
    model: str,
    *,
    provider: str = "",
    cfg: dict | None = None,
    native: int | None = None,
) -> int:
    """Ventana que el modelo realmente va a tener en este request.

    Es el minimo entre lo que el modelo soporta y el `num_ctx` que pedimos: pedir
    mas de lo que soporta es 400; asumir mas de lo que pedimos es overflow.
    """
    nat = native if native else known(model)
    if nat is None:
        # Sin dato del modelo: si somos nosotros los que fijamos num_ctx, ese manda
        # (es lo que Ollama va a usar); cloud asume ventana grande.
        pedido = config_num_ctx(cfg)
        if pedido:
            return pedido
        return DEFAULT_CLOUD_CONTEXT if provider and provider != "ollama" else DEFAULT_CONTEXT
    pedido = config_num_ctx(cfg)
    if provider == "ollama" and pedido:
        return min(nat, pedido)
    return nat


def clamp_num_ctx(model: str, num_ctx: int) -> int:
    """Baja `num_ctx` al limite nativo del modelo si lo supera (evita el 400)."""
    try:
        n = int(num_ctx or 0)
    except (TypeError, ValueError):
        return num_ctx
    nat = known(model)
    if n and nat and n > nat:
        logger.info("context: num_ctx %d > nativo %d en %s; uso %d", n, nat, model, nat)
        return nat
    return n


def default_cache_path() -> Path:
    return Path(__file__).parents[2] / "data" / "context_limits.json"


# Patrones con los que los proveedores dicen cual era el limite real. Ollama suele
# responder "the input length exceeds the context length" sin numero: en ese caso
# el que llama tiene que reducir a ciegas (ver agent_loop).
_LIMIT_PATTERNS = (
    r"context[_\s]?length\D*?(\d{3,7})",
    r"context[_\s]?limit\D*?(\d{3,7})",
    r"context window\D*?(\d{3,7})",
    r"max[_\s]?tokens\D*?(\d{3,7})",
    r"too long\D*?(\d{3,7})",
    r"maximum.*?(\d{3,7})",
    r"exceed\w*\D{0,40}?(\d{3,7})",
)


def limit_from_error(text: str) -> int | None:
    """Saca el limite de contexto del mensaje de error, si lo trae."""
    import re

    blob = str(text or "")
    if not blob:
        return None
    for pat in _LIMIT_PATTERNS:
        m = re.search(pat, blob, re.I)
        if not m:
            continue
        try:
            n = int(m.group(1))
        except (TypeError, ValueError):
            continue
        # Piso bajo a proposito: los errores sinteticos de tests y algunos
        # proveedores chicos reportan ventanas de 3 cifras. El objetivo es no
        # confundir un overflow con otro numero (por eso hay techo).
        if 100 <= n <= 4_000_000:
            return n
    return None
