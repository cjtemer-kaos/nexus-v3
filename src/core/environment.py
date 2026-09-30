"""Autodetección de entorno — portable por diseño.

Todo lo que depende de la PC concreta (Ollama, modelos instalados, OS,
rutas de datos) se resuelve aquí en tiempo de ejecución: env vars +
config + lo que el Ollama local reporta en /api/tags.

Reglas de portabilidad (2026-09-29):
- Cero rutas absolutas de esta máquina en el código.
- Cero vínculos fuera de la carpeta del proyecto (los datos viven en
  ./data y ./brain; NEXUS_BRAIN es un enlace externo opcional y de solo
  lectura, explicitado por env).
- Copiar la carpeta a otra PC (distinto SO, distinto Ollama) y funciona:
  detecta qué hay y se adapta.
"""
from __future__ import annotations

import logging
import os
import platform
import time
from pathlib import Path
from typing import Optional, Sequence

logger = logging.getLogger("nexus-env")

DEFAULT_OLLAMA = "http://localhost:11434"
_TAGS_TTL_S = 60.0
_tags_cache: dict = {"url": "", "ts": 0.0, "models": []}


def workspace_root() -> Path:
    """Raíz del proyecto (carpeta que contiene src/ y config/)."""
    return Path(__file__).resolve().parents[2]


def data_home() -> Path:
    """Directorio de datos interno del proyecto (./data).

    Por defecto SIEMPRE dentro de la carpeta del proyecto. La env var
    NEXUS_V3_DATA permite redirigirlo explícitamente (opt-in).
    """
    custom = os.environ.get("NEXUS_V3_DATA")
    p = Path(custom).expanduser() if custom else (workspace_root() / "data")
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def os_name() -> str:
    """windows | linux | darwin | otro."""
    return (platform.system() or "unknown").lower()


def ollama_base_url(cfg_base: Optional[str] = None) -> str:
    """URL del Ollama local: OLLAMA_HOST > config > default.

    Normaliza host sin esquema: "127.0.0.1:11434" -> "http://127.0.0.1:11434".
    """
    raw = os.environ.get("OLLAMA_HOST") or cfg_base or DEFAULT_OLLAMA
    raw = str(raw).strip().rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw
    return raw


def list_ollama_models(base_url: Optional[str] = None, timeout: float = 1.5, force: bool = False) -> list[str]:
    """Modelos instalados en el Ollama local (GET /api/tags).

    Devuelve [] si Ollama no responde (la decisión de fallback la toma el
    caller). Cache de 60s para no golpear /api/tags en cada probe.
    """
    url = (base_url or ollama_base_url()).rstrip("/")
    now = time.time()
    if not force and _tags_cache["url"] == url and (now - float(_tags_cache["ts"])) < _TAGS_TTL_S:
        return list(_tags_cache["models"])  # type: ignore[arg-type]
    models: list[str] = []
    try:
        import httpx
        r = httpx.get(f"{url}/api/tags", timeout=timeout)
        if r.status_code == 200:
            models = [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]
    except Exception as e:
        logger.debug("list_ollama_models(%s) failed: %s", url, e)
    _tags_cache.update({"url": url, "ts": now, "models": models})
    return list(models)


def family_base(name: str) -> str:
    """Familia del modelo: id sin tag (deepseek-r1:8b -> deepseek-r1)."""
    return str(name).split(":")[0].lower()


def family_match(name: str, available: Sequence[str]) -> Optional[str]:
    """Exacto o por familia contra los modelos reales. Devuelve el id real o None."""
    if name in available:
        return name
    b = family_base(name)
    for a in available:
        if family_base(a) == b:
            return a
    return None


def is_embed(name: str) -> bool:
    n = str(name).lower()
    return "embed" in n or "nomic" in n or n.startswith("bge") or "minilm" in n


def adapt_models(configured: Sequence[str], available: Sequence[str]) -> tuple[list[str], list[str]]:
    """Interseca los modelos de config con los reales de ESTA PC.

    - Sin detección (Ollama apagado / primer arranque): se respeta la
      config tal cual y el provider resuelve en cada call.
    - Match exacto/familia: se registra el id de config (el provider lo
      resuelve al id real en runtime).
    - Cero matches y hay modelos no-embed: se adoptan todos los
      disponibles (PC nueva con otros modelos).
    - Si todo lo registrado es embed y hay chat disponible: se agregan
      los chat disponibles.

    Devuelve (registrados, descartados).
    """
    configured = list(configured or [])
    available = list(available or [])
    if not available:
        return configured, []

    resolved: list[str] = []
    seen_families: set[str] = set()
    dropped: list[str] = []
    for c in configured:
        if family_match(c, available) and family_base(c) not in seen_families:
            resolved.append(c)
            seen_families.add(family_base(c))
        elif not family_match(c, available):
            dropped.append(c)

    if not resolved:
        adopted = [a for a in available if not is_embed(a)]
        if adopted:
            logger.info(
                "adapt_models: 0/%d modelos de config existen aquí; adoptados %d del entorno",
                len(configured), len(adopted),
            )
            return adopted, dropped
        return resolved, dropped

    if not any(not is_embed(m) for m in resolved):
        extra = [a for a in available if not is_embed(a) and family_base(a) not in seen_families]
        if extra:
            resolved = extra + resolved
    return resolved, dropped


def pick_embed_model(configured: str, available: Sequence[str]) -> str:
    """Mejor modelo de embeddings para esta PC."""
    if not available:
        return configured
    if family_match(configured, available):
        return configured
    for a in available:
        if is_embed(a):
            return a
    return configured


def pick_model(available: Sequence[str], prefer: Sequence[str] = (), exclude_embed: bool = True) -> Optional[str]:
    """Primer modelo disponible, priorizando familias de `prefer`."""
    if not available:
        return None
    for pref in prefer:
        pb = family_base(pref)
        for a in available:
            ab = family_base(a)
            if ab == pb or ab.startswith(pb) or str(a).startswith(pref):
                return a
    for a in available:
        if exclude_embed and is_embed(a):
            continue
        return a
    return None
