"""Descubrimiento y cache de modelos por proveedor BYOK.

Mantiene el selector actualizado consultando /v1/models de cada proveedor con
key, con cache TTL en disco (data/provider_models.json). Evita hardcodear
listas que envejecen (ej. opencode-go cambiaba sus modelos).

- cached_models(pid): lista cacheada (o None si falta/vencida segun TTL).
- refresh_all(force): consulta /v1/models de cada proveedor BYOK bearer con key
  y actualiza el cache; ante fallo conserva el valor previo.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_TTL_S = float(os.environ.get("NEXUS_PROVIDER_MODELS_TTL", "900"))  # 15 min

# Filtro de modelos que no son de chat (no deben aparecer en el selector).
_NOISE = (
    "embed", "whisper", "tts", "dall-e", "dall·e", "moderation",
    "rerank", "audio", "speech", "image-", "-vision-embed",
    "orpheus", "prompt-guard", "safeguard", "guard-",
)


def _cache_path() -> Path:
    raw = os.environ.get("NEXUS_PROVIDER_MODELS_CACHE")
    if raw:
        p = Path(raw).expanduser()
        if p.is_absolute():
            return p
        from src.core.environment import workspace_root
        return workspace_root() / p
    # Absoluto al proyecto: "./data" dependía del CWD del proceso.
    from src.core.environment import data_home
    return data_home() / "provider_models.json"


def is_chat_model(mid: str) -> bool:
    m = (mid or "").strip().lower()
    if not m:
        return False
    return not any(n in m for n in _NOISE)


def load_cache() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_cache(d: dict[str, Any]) -> None:
    try:
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def cached_models(pid: str, max_age_s: float | None = None) -> list[str] | None:
    """Modelos cacheados del proveedor (chat-only). None si falta o venció el TTL.
    max_age_s=-1 ignora la edad (para routing: usar lo ultimo conocido)."""
    entry = load_cache().get(pid)
    if not isinstance(entry, dict):
        return None
    ttl = _TTL_S if max_age_s is None else max_age_s
    age = time.time() - float(entry.get("ts", 0) or 0)
    if ttl >= 0 and age > ttl:
        return None
    return [m for m in (entry.get("models") or []) if is_chat_model(m)]


def is_stale(pid: str) -> bool:
    entry = load_cache().get(pid)
    if not isinstance(entry, dict):
        return True
    return (time.time() - float(entry.get("ts", 0) or 0)) > _TTL_S


def _byok_bearer_providers():
    from src.core.provider_catalog import PROVIDER_CATALOG
    out = []
    for pc in PROVIDER_CATALOG:
        env = pc.get("api_key_env") or ""
        if not env or pc.get("auth_style") != "bearer":
            continue
        base = (pc.get("base_url") or "").rstrip("/")
        if not base.endswith("/v1") or pc["id"] == "cohere":
            continue
        out.append((pc["id"], base[:-3], env, pc))
    return out


def byok_ids() -> set[str]:
    return {pid for pid, _base, _env, _pc in _byok_bearer_providers()}


async def refresh_all(force: bool = False) -> dict[str, list[str]]:
    """Consulta /v1/models de cada proveedor BYOK con key y actualiza el cache.
    Devuelve {pid: [modelos]} de los que se refrescaron."""
    from src.providers.cloud_provider import CloudProvider
    cache = load_cache()
    updated: dict[str, list[str]] = {}
    for pid, base, env, pc in _byok_bearer_providers():
        if not force and not is_stale(pid):
            continue
        key = os.environ.get(env, "")
        if not key:
            continue
        prov = None
        try:
            prov = CloudProvider(base_url=base, api_key=key, auth_style="bearer", timeout_s=12)
            ids = [m for m in (await prov.list_models()) if is_chat_model(m)]
            # Unir listas extra (ej. opencode-go incluye el catalogo Zen completo).
            for extra_base in (pc.get("models_union_from") or []):
                ep = None
                try:
                    ep = CloudProvider(base_url=str(extra_base).rstrip("/"), api_key=key, auth_style="bearer", timeout_s=12)
                    for m in (await ep.list_models()):
                        if is_chat_model(m) and m not in ids:
                            ids.append(m)
                except Exception:
                    pass
                finally:
                    if ep is not None:
                        try:
                            await ep.close()
                        except Exception:
                            pass
            if ids:
                for m in (pc.get("extra_models") or []):
                    if m not in ids:
                        ids.append(m)
                cache[pid] = {"models": ids, "ts": time.time()}
                updated[pid] = ids
        except Exception:
            # ante fallo, conservar el cache previo (no perder el ultimo conocido)
            pass
        finally:
            if prov is not None:
                try:
                    await prov.close()
                except Exception:
                    pass
    save_cache(cache)
    return updated
