"""
Nexus v3 Harness Backend — FastAPI server for Electron.

Serves:
  - /api/health      -> health + config diagnostics
  - /api/config      -> config.yaml content
  - /api/sessions    -> SessionLog CRUD (WAL+FTS5)
  - /api/chat        -> AgentLoop TDAO (ProviderRouter + ToolRegistry + Scope)
  - /api/brain/*     -> Brain link diagnostics
  - Static SPA       -> ui/dist (production) or proxy to Vite dev (5174)

Port: auto-assigned by Electron (free port) via --port, default 8765.
CORS: enabled for Electron renderer + dev Vite.

Run:
  python -m src.api.server --port 8765
  python -m src.api.server --port 0  # random free

No hardcoding: reads config/config.yaml via src.core.config
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse as _urlparse

# Ensure project root on sys.path so `import src.*` works when spawned from Electron
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# .env de la raiz del proyecto (portable, sin rutas fijas). override=False para
# respetar variables ya seteadas en el shell (NEXUS_BRAIN, OPENCODE_API_KEY, ...).
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_PROJECT_ROOT / ".env", encoding="utf-8-sig", override=False)
except Exception:  # python-dotenv ausente -> solo env del sistema
    pass

import os
import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import logging
import json as _json2
from src.core.todo_store import TodoStore

logger = logging.getLogger("nexus-v3")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [nexus-v3] %(levelname)s %(message)s")

app = FastAPI(title="Nexus v3 Harness", version="3.0.0-alpha")

# CORS for Electron renderer (file:// and localhost)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Fase 8 (hardening): los endpoints POST no llevan token; con CORS `*` sin
# mas, cualquier pagina web podria invocar /api/exec desde el navegador del
# usuario (CSRF -> ejecucion de comandos). Guardia de Origin para escrituras:
# se permiten llamadas sin Origin (curl/scripts/MCP) y origenes locales.
# Electron file:// envia Origin: null y NO esta permitido aun — si se cablea
# Electron, anadir "null" aqui deliberadamente.
_LOCAL_ORIGIN_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin:
            try:
                host = _urlparse(origin).hostname or ""
            except Exception:
                host = ""
            if host not in _LOCAL_ORIGIN_HOSTS:
                return JSONResponse(
                    status_code=403,
                    content={"error": f"origen no permitido para escritura: {origin}"},
                )
    return await call_next(request)

# Lazy singletons populated on startup
_state: dict[str, Any] = {}


def _load_config_safe() -> dict:
    try:
        from src.core.config import load_config, validate_config
        cfg = load_config()
        errs = validate_config(cfg)
        cfg["_validation_errors"] = errs
        return cfg
    except Exception as e:
        return {"_error": str(e), "_validation_errors": [str(e)]}


def _brain_info_safe() -> dict:
    try:
        from src.brain.link import get_brain_info, brain_path
        info = get_brain_info()
        info["brain_path"] = str(brain_path())
        return info
    except Exception as e:
        return {"error": str(e)}


def _session_log():
    if "session_log" not in _state:
        from src.core.session_log import SessionLog
        from src.core.config import load_config
        try:
            cfg = load_config()
            db_path = cfg.get("session", {}).get("db_path", "./data/session.db")
            # Resolve relative to project root
            p = Path(db_path)
            if not p.is_absolute():
                p = _PROJECT_ROOT / p
            wal = cfg.get("session", {}).get("wal", True)
            _state["session_log"] = SessionLog(str(p), wal=wal)
        except Exception as e:
            logger.warning(f"SessionLog init failed, using memory: {e}")
            from src.core.session_log import SessionLog
            _state["session_log"] = SessionLog(":memory:")
    return _state["session_log"]


@app.on_event("startup")
async def _startup():
    logger.info("Starting Nexus v3 harness backend")
    _state["config"] = _load_config_safe()
    _state["brain"] = _brain_info_safe()
    # Resumen de entorno: qué hay en ESTA PC (SO, Ollama, modelos, brain)
    try:
        from src.core import environment as _env
        _ocfg = (_state["config"].get("providers", {}) or {}).get("ollama", {}) or {}
        _base = _env.ollama_base_url(_ocfg.get("base_url"))
        _tags = _env.list_ollama_models(_base)
        logger.info(
            "Entorno detectado: os=%s root=%s ollama=%s modelos_ollama=%d brain=%s",
            _env.os_name(), _env.workspace_root(), _base, len(_tags),
            (_state["brain"] or {}).get("brain_path", "?"),
        )
    except Exception as e:
        logger.warning(f"environment detect failed: {e}")
    # Touch session log so DB is created early
    try:
        _session_log()
        logger.info(f"SessionLog ready: {_state['brain'].get('brain_path')}")
    except Exception as e:
        logger.warning(f"SessionLog startup warning: {e}")
    # llama.cpp DESCARTADO (2026-09-28): autostart solo si config lo pide explicito.
    # Sin bloque llamacpp en config -> ni se intenta.
    try:
        import os as _os
        _os.environ.setdefault("NEXUS_ROOT", str(_PROJECT_ROOT))
        lc = (_state["config"].get("providers", {}) or {}).get("llamacpp", {}) or {}
        if lc and lc.get("autostart", False):
            from src.providers import llama_manager as _lm
            res = _lm.ensure_residents()
            logger.info(f"llama.cpp residentes: {res}")
            _state["llama"] = res
            _lm.start_idle_watcher()
            logger.info("llama.cpp watcher idle activo (libera a residente tras idle)")
    except Exception as e:
        logger.warning(f"llama.cpp autostart omitido: {e}")
    # Zen free auto-discovery en background — no bloquea startup si falla
    try:
        async def _bg_zen():
            try:
                router = _build_router_from_config(_state["config"])
                # fuerza refresh verificado (ping real) y deja cache en disco
                if hasattr(router, "refresh_cloud_free_models"):
                    free = await router.refresh_cloud_free_models(force=True, verify=True)
                    if free:
                        logger.info(f"Zen free auto-discovery startup (verified): {free}")
                    # cerrar providers temporales
                    for p in router.providers().values():
                        try:
                            await p.close()
                        except Exception:
                            pass
            except Exception as e:
                logger.debug(f"Zen bg discovery failed: {e}")
        import asyncio as _asyncio
        _asyncio.create_task(_bg_zen())
    except Exception:
        pass
    # LearningService (re-wiring ciclo #113, colmena #132): orden EXPLICITO
    # post-arranque, sin actor system. Nunca debe tumbar el startup.
    try:
        from src.core.learning_service import get_learning_service
        _ls = get_learning_service()
        await _ls.start(dream_interval_s=86400.0, enable_dream=True)
        logger.info("LearningService started (judge+Thompson+grafo+dream scheduler)")
    except Exception as e:
        logger.warning(f"LearningService startup skipped: {e}")


@app.on_event("shutdown")
async def _shutdown():
    try:
        from src.core.learning_service import get_learning_service
        await get_learning_service().stop()
        logger.info("LearningService detenida")
    except Exception as e:
        logger.warning(f"learning shutdown: {e}")
    try:
        from src.providers import llama_manager as _lm
        _lm.stop_managed()
        logger.info("llama.cpp gestionados detenidos")
    except Exception as e:
        logger.warning(f"llama shutdown: {e}")


def _build_router_from_config(cfg: dict):
    """Construct ProviderRouter from config dict without health probing."""
    from src.core.config import load_config as _lc  # noqa
    from src.core.provider_router import ProviderRouter, ModelRoute
    from src.providers.ollama_provider import OllamaProvider
    from src.providers.llamacpp_provider import LlamaCppProvider
    from src.providers.cloud_provider import CloudProvider

    prov_cfg = cfg.get("providers", {}) if isinstance(cfg, dict) else {}
    _vram = cfg.get("router", {}).get("vram_budget_gb", 0) if isinstance(cfg, dict) else 0
    if not _vram:
        try:
            from src.core.hardware import auto_vram_budget
            _vram = auto_vram_budget()
        except Exception:
            _vram = 12
    router = ProviderRouter(
        vram_budget_gb=_vram,
        fallback_order=cfg.get("router", {}).get("fallback_order", ["ollama", "cloud"]) if isinstance(cfg, dict) else ["ollama", "cloud"],
    )
    if "ollama" in prov_cfg:
        oc = prov_cfg["ollama"]
        from src.core import environment as _env
        _base = _env.ollama_base_url(oc.get("base_url"))
        _avail = _env.list_ollama_models(_base)
        _resolved, _dropped = _env.adapt_models(oc.get("models") or [], _avail)
        if _avail:
            logger.info(
                "Ollama en %s: %d modelos detectados; registrados %d%s",
                _base, len(_avail), len(_resolved),
                f"; no existen aquí: {_dropped}" if _dropped else "",
            )
        else:
            logger.warning(f"Ollama no responde en {_base} — se registran los modelos de config (se resuelven en cada call)")
        router.register_provider("ollama", OllamaProvider(base_url=_base, timeout_s=oc.get("timeout_s", 300)))
        for m in _resolved:
            router.register_model(ModelRoute(id=m, provider_name="ollama"))
    if "llamacpp" in prov_cfg:
        lc = prov_cfg["llamacpp"]
        router.register_provider("llamacpp", LlamaCppProvider(base_url=lc.get("base_url", "http://localhost:8080"), fallback_url=lc.get("fallback_url"), timeout_s=lc.get("timeout_s", 300)))
        for m in lc.get("models", []) or []:
            router.register_model(ModelRoute(id=m, provider_name="llamacpp"))
    if "cloud" in prov_cfg:
        cc = prov_cfg["cloud"]
        if cc.get("enabled"):
            router.register_provider("cloud", CloudProvider(base_url=cc.get("base_url"), api_key=cc.get("api_key"), auth_style=cc.get("auth_style", "bearer")))
            for m in cc.get("models", []) or []:
                router.register_model(ModelRoute(id=m, provider_name="cloud"))
    # DECK: proveedores BYOK del catalogo — se registran solo con key en env/.env.
    # CloudProvider anade /v1/chat/completions al base_url, asi que se recorta el
    # sufijo /v1 del catalogo. Solo auth bearer (compat OpenAI); cohere/gemini/
    # anthropic tienen formatos distintos -> no se registran aqui.
    try:
        from src.core.provider_catalog import PROVIDER_CATALOG
        for pc in PROVIDER_CATALOG:
            env_name = pc.get("api_key_env") or ""
            if not env_name or pc.get("auth_style") != "bearer":
                continue
            key = os.environ.get(env_name, "")
            if not key:
                continue
            base = (pc.get("base_url") or "").rstrip("/")
            if not base.endswith("/v1") or pc["id"] == "cohere":
                continue
            pid = pc["id"]
            router.register_provider(pid, CloudProvider(base_url=base[:-3], api_key=key, auth_style="bearer"))
            router.register_model(ModelRoute(id=pc["default_model"], provider_name=pid))
            if pid not in router.fallback_order:
                router.fallback_order.append(pid)
            logger.info(f"BYOK catalogo activo: {pid} ({pc['default_model']})")
    except Exception as e:
        logger.warning(f"catalogo BYOK no registrado: {e}")
    return router


async def _probe_providers(timeout_s: float = 2.5) -> dict:
    """Live health probe — returns {name: {ok, models, status, error}} without mutating global router."""
    cfg = _state.get("config") or _load_config_safe()
    try:
        router = _build_router_from_config(cfg if isinstance(cfg, dict) else {})
        # Antes de health: sincroniza gratis vivos (usa cache TTL 5m, no golpea Zen cada segundo)
        try:
            await router.refresh_cloud_free_models(force=False)
        except Exception:
            pass
        # health_all does parallel probes with timeout per provider
        health = await asyncio.wait_for(router.health_all(), timeout=timeout_s + 2)
        # Normalize to status string
        out: dict[str, dict] = {}
        for name, info in health.items():
            ok = bool(info.get("ok"))
            out[name] = {
                "ok": ok,
                "status": "online" if ok else "offline",
                "models": info.get("models", []),
                "registered": info.get("registered", []),
                "error": info.get("error"),
            }
        # Providers configured but not probed (e.g. cloud disabled) -> omit
        return out
    except asyncio.TimeoutError:
        return {"error": "probe timeout"}
    except Exception as e:
        logger.warning(f"probe providers failed: {e}")
        return {"error": str(e)}


@app.get("/api/health")
async def health():
    cfg = _state.get("config") or _load_config_safe()
    brain = _state.get("brain") or _brain_info_safe()
    providers_ok: dict = {}
    try:
        # Lightweight live probe but with very short timeout so health stays fast
        providers_ok = await _probe_providers(timeout_s=2.0)
    except Exception as e:
        providers_ok = {"error": str(e)}
    return {
        "status": "ok",
        "service": "nexus-v3-harness",
        "version": "3.0.0-alpha",
        "project": cfg.get("project") if isinstance(cfg, dict) else "nexus-v3",
        "valid": cfg.get("_validation_errors") == [] if isinstance(cfg, dict) else False,
        "validation_errors": cfg.get("_validation_errors") if isinstance(cfg, dict) else [],
        "brain": brain,
        "providers": providers_ok,
    }


@app.get("/api/providers")
async def providers_status():
    """Dynamic provider inventory — only online providers report models. Used by ModelSelector."""
    cfg = _state.get("config") or _load_config_safe()
    probe = await _probe_providers(timeout_s=3.0)
    # Build flat model list for UI: only from online providers
    models: list[dict] = []
    # ctx map: prefer config ctx for llamacpp, else default
    ctx_default = 32768
    llamacpp_ctx = 40960
    try:
        if isinstance(cfg, dict):
            llamacpp_ctx = cfg.get("providers", {}).get("llamacpp", {}).get("ctx", 40960) or 40960
    except Exception:
        pass
    label_overrides: dict[str, str] = {
        "nexus-director-v6": "Nexus Director v6",
        "qwen3.5-9b": "Qwen 3.5 9B",
        "qwen3-vl-8b": "Qwen3 VL 8B",
        "qwen3-vl-4b": "Qwen3 VL 4B",
        "qwen2.5-coder:7b": "Qwen2.5 Coder 7B",
        "deepseek-r1-8b": "DeepSeek R1 8B",
        "deepseek-r1:8b": "DeepSeek R1 8B",
        "omnicoder-2-9b": "Omnicoder 2 9B",
        "nemotron-3-nano-4b": "Nemotron 3 Nano 4B",
        "nemotron-3-ultra-free": "Nemotron 3 Ultra (Zen)",
        "nemotron-3.5-lightning-free": "Nemotron 3.5 Lightning (Zen)",
        "ling-3.0-flash-fin-free": "Ling 3.0 Flash (Zen)",
        "nomic-embed-text": "Nomic Embed",
    }
    import re as _re
    def _pretty_id(mid: str) -> tuple[str, str]:
        """Normaliza ID en vivo (ruta GGUF) a id corto de catálogo + etiqueta.
        Ej: /ruta/modelos/nexus-director-v6-Q8_0.gguf -> (nexus-director-v6, Nexus Director v6)."""
        base = _re.split(r"[\\/]", mid)[-1]
        if base.lower().endswith(".gguf"):
            base = base[:-5]
        # quita sufijos de cuantización: -Q8_0, -Q4_K_M, -Q5_K_M, -IQ4_XS, -f16...
        short = _re.sub(r"-(Q\d(_\d)?|Q\d_K_[SM]|IQ\d_[XXSM]+|F16|BF16)$", "", base, flags=_re.IGNORECASE)
        label = label_overrides.get(short, label_overrides.get(base, None))
        if label is None:
            # bonito por defecto: guiones -> espacios, capitaliza
            label = short.replace("-", " ").replace("_", " ").strip().title() or base
        return short, label
    for pname, info in probe.items():
        if pname == "error":
            continue
        if not info.get("ok"):
            continue  # offline -> don't expose models
        for mid in info.get("registered") or info.get("models") or []:
            # Usar modelos CONFIGURADOS (registered) primero, no el catálogo completo
            # en vivo (evita exponer Claude Opus 5 / GPT-6 / etc. en el selector).
            # Deduplicate
            if any(m["id"] == mid for m in models):
                continue
            ctx = llamacpp_ctx if pname == "llamacpp" else 32000 if "coder" in mid or "deepseek" in mid else 32000
            # embed model should not appear as chat model
            if "embed" in mid.lower() and "nomic" in mid.lower():
                continue
            short_id, label = _pretty_id(mid)
            if any(m["id"] == short_id for m in models):
                continue
            models.append({
                "id": short_id,
                "provider": pname,
                "label": label,
                "ctx": ctx,
                "status": "online",
            })
        # Also include registered models that are online but not yet listed? already covered
    return {
        "providers": probe,
        "models": models,
        "router": cfg.get("router") if isinstance(cfg, dict) else {},
    }


@app.get("/api/cloud/free-models")
async def cloud_free_models(force: bool = False, verify: bool = False):
    """Lista gratis detectados en Zen. force=ignora TTL, verify=true hace ping real por modelo (filtra deepseek off)."""
    cfg = _state.get("config") or _load_config_safe()
    cc = (cfg.get("providers", {}) or {}).get("cloud", {}) if isinstance(cfg, dict) else {}
    base_url = cc.get("base_url", "https://opencode.ai/zen")
    api_key = cc.get("api_key", "${OPENCODE_API_KEY}")
    auth_style = cc.get("auth_style", "bearer")
    from src.providers.cloud_provider import CloudProvider
    prov = CloudProvider(base_url=base_url, api_key=api_key, auth_style=auth_style, timeout_s=14)
    try:
        live = await prov.list_free_models(force=force, verify=verify)
        health = None
        if verify:
            # health detallado para UI debug
            raw = await prov.list_free_models(force=force, verify=False)
            # raw = todos los *free de /v1/models
            try:
                raw_ids = [m for m in raw]  # raw es ya lista
                # si live != raw, hace probe para mostrar qué cayó
                if set(raw_ids) != set(live):
                    health = await prov.probe_free_models_health(raw_ids)
                elif live:
                    health = await prov.probe_free_models_health(live)
            except Exception:
                pass
        registered = cc.get("models", []) or []
        return {"free_models": live, "health": health, "registered": registered, "source": base_url, "count": len(live), "verified": verify}
    finally:
        try:
            await prov.close()
        except Exception:
            pass


@app.get("/api/config")
async def get_config():
    cfg = _state.get("config") or _load_config_safe()
    return JSONResponse(cfg)


# ---- Providers config (editable) ----
def _mask_api_key(v: str | None) -> str:
    if not v:
        return ""
    if v.startswith("${") and v.endswith("}"):
        return v  # keep env ref visible but don't leak resolved value
    if len(v) <= 8:
        return "••••••••"
    return v[:4] + "••••" + v[-4:]


@app.get("/api/providers/config")
async def get_providers_config():
    """Editable providers config — raw (not env-expanded) + masked api_key."""
    try:
        from src.core.config import load_raw_config
        raw = load_raw_config()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    providers = raw.get("providers") or {}
    # Mask api_key for transport but keep flag has_key
    masked: dict[str, dict] = {}
    for name, pcfg in providers.items():
        if not isinstance(pcfg, dict):
            continue
        m = dict(pcfg)
        if "api_key" in m:
            raw_key = str(m["api_key"] or "")
            m["api_key_masked"] = _mask_api_key(raw_key)
            # expose whether key is set via env ref
            m["_has_env_key"] = raw_key.startswith("${")
            # Don't send raw expanded key if it's an env ref resolved; keep ref
            # For UI editing, send empty if env-resolved, or masked
            if raw_key.startswith("${"):
                m["api_key"] = ""  # must re-enter to override env
            else:
                m["api_key"] = raw_key
        masked[name] = m
    return {"providers": masked, "router": raw.get("router") or {}}


@app.put("/api/providers/config")
async def put_providers_config(request: Request):
    """Save providers (+router). Validates and writes config.yaml."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    providers_in = body.get("providers")
    router_in = body.get("router")
    if not isinstance(providers_in, dict):
        raise HTTPException(status_code=400, detail="providers must be object")
    try:
        from src.core.config import load_raw_config, save_config, validate_config
        raw = load_raw_config()
        # Merge providers — allow partial update
        current_providers = raw.get("providers") or {}
        for pname, pval in providers_in.items():
            if not isinstance(pval, dict):
                continue
            # Handle api_key: if empty string and existing had env ref, keep env ref
            existing = current_providers.get(pname) or {}
            merged = dict(existing)
            for k, v in pval.items():
                if k == "api_key_masked" or k == "_has_env_key":
                    continue
                if k == "api_key" and (v == "" or v is None):
                    # Keep existing if was env ref or masked placeholder
                    if existing.get("api_key", "").startswith("${"):
                        continue
                    # if user cleared api_key, allow clearing
                    if v == "":
                        # check if they intended to keep masked value: if api_key_masked present and v empty, keep existing
                        if "_has_env_key" not in pval and existing.get("api_key"):
                            # keep existing unless explicitly cleared via _clear flag
                            # For UX: empty means keep existing
                            continue
                    merged[k] = v
                elif k == "models" and isinstance(v, str):
                    # UI may send comma string
                    merged[k] = [s.strip() for s in v.split(",") if s.strip()]
                else:
                    merged[k] = v
            # Normalize models to list
            if "models" in merged and isinstance(merged["models"], str):
                merged["models"] = [s.strip() for s in merged["models"].split(",") if s.strip()]
            # Normalize base_url trimming
            if "base_url" in merged and isinstance(merged["base_url"], str):
                merged["base_url"] = merged["base_url"].strip().rstrip("/")
                if merged["base_url"] and not merged["base_url"].startswith("http"):
                    merged["base_url"] = "https://" + merged["base_url"]
            current_providers[pname] = merged
        raw["providers"] = current_providers
        if isinstance(router_in, dict):
            raw_router = raw.get("router") or {}
            raw_router.update(router_in)
            raw["router"] = raw_router
        errs = validate_config(raw)
        if errs:
            raise HTTPException(status_code=400, detail={"validation_errors": errs})
        save_config(raw)
        # Refresh cached config
        _state["config"] = _load_config_safe()
        return {"ok": True, "providers": raw["providers"], "router": raw.get("router")}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("save providers failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/providers/test")
async def test_provider(request: Request):
    """Test connection without saving. Body: {provider, base_url, api_key, auth_style, fallback_url?}"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    provider = (body.get("provider") or body.get("name") or "custom").strip().lower() or "custom"
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    api_key = body.get("api_key") or ""
    auth_style = body.get("auth_style") or "bearer"
    fallback_url = (body.get("fallback_url") or "").strip().rstrip("/") or None
    if not base_url:
        raise HTTPException(status_code=400, detail="base_url required")
    # Resolve env ref if api_key is ${VAR}
    if isinstance(api_key, str) and api_key.startswith("${"):
        import re, os
        m = re.match(r"\$\{([^:}]+)(?::-[^}]+)?\}", api_key)
        if m:
            api_key = os.environ.get(m.group(1), "")
    import time
    t0 = time.monotonic()
    ok = False
    models: list[str] = []
    error: str | None = None
    try:
        prov = None
        if provider == "ollama":
            from src.providers.ollama_provider import OllamaProvider
            prov = OllamaProvider(base_url=base_url, timeout_s=6)
        elif provider in ("llamacpp", "llama.cpp", "llama_cpp"):
            from src.providers.llamacpp_provider import LlamaCppProvider
            prov = LlamaCppProvider(base_url=base_url, fallback_url=fallback_url, timeout_s=6)
        else:
            from src.providers.cloud_provider import CloudProvider
            prov = CloudProvider(base_url=base_url, api_key=api_key, auth_style=auth_style, timeout_s=10)
        try:
            ok = await asyncio.wait_for(prov.health(), timeout=8)
        except Exception as e:
            error = str(e)
            ok = False
        if ok:
            try:
                models = await asyncio.wait_for(prov.list_models(), timeout=8)
            except Exception as e:
                error = str(e)
                models = []
        try:
            await prov.close()
        except Exception:
            pass
    except Exception as e:
        error = str(e)
        ok = False
    latency = int((time.monotonic() - t0) * 1000)
    return {"ok": ok, "status": "online" if ok else "offline", "models": models, "error": error, "latency_ms": latency, "provider": provider, "base_url": base_url}


# ---- Knowledge Graph (knowledge_graph.db via brain link) ----
import sqlite3

_GRAPH_CACHE: dict[str, Any] = {"ts": 0, "data": None}

def _compute_pagerank(node_ids: list[int], edges: list[tuple[int,int]], damping: float = 0.85, iters: int = 18) -> dict[int, float]:
    n = len(node_ids)
    if n == 0:
        return {}
    idx = {nid: i for i, nid in enumerate(node_ids)}
    out_deg = [0]*n
    in_edges: list[list[int]] = [[] for _ in range(n)]
    for u, v in edges:
        if u in idx and v in idx:
            ui, vi = idx[u], idx[v]
            out_deg[ui] += 1
            in_edges[vi].append(ui)
    pr = [1.0/n]*n
    for _ in range(iters):
        new_pr = [(1-damping)/n]*n
        dangling_sum = sum(pr[i] for i in range(n) if out_deg[i]==0)
        dangling_contrib = damping * dangling_sum / n
        for i in range(n):
            new_pr[i] += dangling_contrib
            s = 0.0
            for j in in_edges[i]:
                if out_deg[j] > 0:
                    s += pr[j] / out_deg[j]
            new_pr[i] += damping * s
        pr = new_pr
    return {nid: pr[idx[nid]] for nid in node_ids}

def _detect_communities(node_ids: list[int], edges: list[tuple[int,int]], iters: int = 5) -> dict[int, int]:
    # Label propagation on undirected graph
    if not node_ids:
        return {}
    adj: dict[int, list[int]] = {nid: [] for nid in node_ids}
    node_set = set(node_ids)
    for u, v in edges:
        if u in node_set and v in node_set:
            adj[u].append(v)
            adj[v].append(u)
    # init each node its own label
    label: dict[int, int] = {nid: nid for nid in node_ids}
    import random
    # sort to make deterministic but shuffle each iter
    for _ in range(iters):
        order = node_ids[:]
        random.shuffle(order)
        for nid in order:
            nbrs = adj.get(nid) or []
            if not nbrs:
                continue
            # most frequent neighbor label
            freq: dict[int,int] = {}
            for nb in nbrs:
                l = label[nb]
                freq[l] = freq.get(l, 0) + 1
            if freq:
                # tie-break by smallest label for determinism if same freq
                best = max(freq.items(), key=lambda x: (x[1], -x[0]))[0]
                label[nid] = best
    # compress to dense 0..k-1, sorted by community size desc
    from collections import Counter
    cnt = Counter(label.values())
    # sort communities by size desc, assign new id by size order
    sorted_comms = [c for c,_ in cnt.most_common()]
    remap = {old: new for new, old in enumerate(sorted_comms)}
    return {nid: remap[lbl] for nid, lbl in label.items()}

def _load_graph_data(limit: int = 300, q: str = "", category: str = "", community: str = "", min_importance: int = 0, include_isolated: bool = False) -> dict:
    from src.brain.link import brain_path
    db_path = brain_path() / "knowledge_graph.db"
    if not db_path.exists():
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0, "error": f"knowledge_graph.db not found at {db_path}"}, "communities": []}
    try:
        # NOTE: URI mode=ro falla en SQLite 3.50.4 Windows; solo SELECT así que ruta plana es segura.
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT id, title, category, source, importance, tags, content, access_count FROM nodes ORDER BY id")
        rows = cur.fetchall()
        cur.execute("SELECT id, from_node, to_node, relation, weight, note FROM edges")
        edge_rows = cur.fetchall()
        con.close()
    except Exception as e:
        return {"nodes": [], "edges": [], "stats": {"total_nodes": 0, "total_edges": 0, "error": str(e)}, "communities": []}

    # Build structures
    all_node_ids = [r["id"] for r in rows]
    raw_edges: list[tuple[int,int]] = []
    edge_objs: list[dict] = []
    for r in edge_rows:
        try:
            u = int(r["from_node"]); v = int(r["to_node"])
            raw_edges.append((u, v))
            edge_objs.append({"id": r["id"], "source": u, "target": v, "relation": r["relation"] or "relates_to", "weight": r["weight"] or 0.5, "label": r["relation"] or "relates_to"})
        except Exception:
            continue

    # degree
    from collections import Counter
    deg = Counter()
    for u,v in raw_edges:
        deg[u] += 1
        deg[v] += 1

    # pagerank
    pr_map = _compute_pagerank(all_node_ids, raw_edges)
    # communities
    comm_map = _detect_communities(all_node_ids, raw_edges)

    # Prepare node list with computed fields
    PALETTE = ["#8b5cf6","#06b6d4","#f59e0b","#10b981","#ef4444","#ec4899","#6366f1","#84cc16","#14b8a6","#f97316","#a855f7","#0ea5e9"]
    CATEGORY_COLOR: dict[str,str] = {"general":"#6b7280","architecture":"#8b5cf6","concept":"#06b6d4","deploy":"#f59e0b","fix":"#10b981","fact":"#ec4899","lesson":"#84cc16"}

    nodes_tmp: list[dict] = []
    for r in rows:
        nid = int(r["id"])
        title_raw = (r["title"] or "").strip()
        # clean title: first line, strip markdown
        label = title_raw.split("\n")[0].strip().lstrip("# ").strip()
        if len(label) > 48:
            label = label[:46] + "…"
        if not label:
            label = f"#{nid}"
        content = r["content"] or ""
        pr = pr_map.get(nid, 1/len(all_node_ids) if all_node_ids else 0)
        d = deg.get(nid, 0)
        comm = comm_map.get(nid, 0)
        size = 4 + min(14, (d * 0.9) + (pr * len(all_node_ids) * 18))
        # normalize size 4..18
        size = max(4, min(18, size))
        cat = (r["category"] or "general").strip() or "general"
        color = PALETTE[comm % len(PALETTE)] if len(set(comm_map.values())) > 1 else CATEGORY_COLOR.get(cat, PALETTE[0])
        nodes_tmp.append({
            "id": nid,
            "label": label,
            "title": title_raw[:120],
            "fullTitle": title_raw,
            "category": cat,
            "source": r["source"] or "",
            "importance": int(r["importance"] or 0),
            "tags": r["tags"] or "[]",
            "content": content[:400] if content else "",
            "fullContent": content,
            "accessCount": int(r["access_count"] or 0),
            "pagerank": pr,
            "degree": d,
            "community": comm,
            "size": round(size, 2),
            "color": color,
        })

    # Filter
    filtered = nodes_tmp
    if q:
        ql = q.lower()
        filtered = [n for n in filtered if ql in n["label"].lower() or ql in n["fullTitle"].lower() or ql in n["content"].lower() or ql in n["tags"].lower() or ql in n["category"].lower()]
    if category and category != "all":
        filtered = [n for n in filtered if n["category"] == category]
    if community != "" and community != "all":
        try:
            ci = int(community)
            filtered = [n for n in filtered if n["community"] == ci]
        except:
            pass
    if min_importance > 0:
        filtered = [n for n in filtered if n["importance"] >= min_importance]
    if not include_isolated:
        # keep isolated only if search is active (user explicitly wants)
        if not q:
            filtered = [n for n in filtered if n["degree"] > 0]

    # Sort by pagerank+degree combo, take top limit
    filtered.sort(key=lambda n: (n["pagerank"]*100 + n["degree"]*0.15 + n["importance"]*0.02), reverse=True)
    visible_nodes = filtered[:limit]
    visible_ids = set(n["id"] for n in visible_nodes)

    # Edges among visible nodes
    visible_edges = [e for e in edge_objs if e["source"] in visible_ids and e["target"] in visible_ids]

    # Re-include up to 1-hop neighbors if filtered is too sparse and q is set? keep simple

    # Stats
    all_categories = sorted(set(r["category"] or "general" for r in rows))
    stats = {
        "total_nodes": len(rows),
        "total_edges": len(edge_objs),
        "visible_nodes": len(visible_nodes),
        "visible_edges": len(visible_edges),
        "communities": len(set(comm_map.values())) if comm_map else 0,
        "avg_degree": round(sum(deg.values())/len(all_node_ids), 2) if all_node_ids else 0,
        "max_pagerank": max(pr_map.values()) if pr_map else 0,
        "categories": all_categories,
    }
    # community summary
    from collections import Counter
    comm_counter = Counter(comm_map.get(n["id"], 0) for n in visible_nodes)
    communities = [{"id": cid, "size": comm_counter.get(cid,0), "color": PALETTE[cid % len(PALETTE)], "label": f"Comunidad {cid}"} for cid in sorted(comm_counter.keys())]

    return {"nodes": visible_nodes, "edges": visible_edges, "stats": stats, "communities": communities}

@app.get("/api/graph")
async def graph(limit: int = 300, q: str = "", category: str = "", community: str = "", min_importance: int = 0, include_isolated: str = "false"):
    inc = include_isolated.lower() in ("1","true","yes")
    # clamp limit
    limit = max(10, min(2000, limit))
    data = _load_graph_data(limit=limit, q=q.strip(), category=category.strip(), community=community.strip(), min_importance=min_importance, include_isolated=inc)
    return JSONResponse(data)

@app.post("/api/embed")
async def embed_text(request: Request):
    """Embeddings vía :8001 (nomic-embed autocontenido). Body: {text|texts}."""
    from src.core import embeddings as _emb
    try:
        body = await request.json()
    except Exception:
        body = {}
    texts = body.get("texts", body.get("text", ""))
    if not texts:
        raise HTTPException(status_code=400, detail="text required")
    try:
        vecs = await __import__("asyncio").to_thread(_emb.embed, texts)
        return {"ok": True, "dims": len(vecs[0]) if vecs else 0, "count": len(vecs), "embeddings": vecs}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"embed :8001 no disponible: {e}")


@app.get("/api/rag/search")
async def rag_search(q: str, limit: int = 5):
    """Búsqueda semántica RAG sobre el índice propio."""
    from src.core import rag as _rag
    try:
        import asyncio as _aio
        res = await _aio.to_thread(_rag.search, q, max(1, min(20, limit)))
        return {"query": q, "results": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/rag/index-graph")
async def rag_index_graph(request: Request):
    """Indexa knowledge_graph propio a chunks+embeddings. Body: {limit?, background?}. background=true → retorna inmediato."""
    from src.core import rag as _rag
    try:
        body = await request.json()
    except Exception:
        body = {}
    background = body.get("background")
    if background is None:
        background = (int(body.get("limit", 0) or 0) == 0)
    else:
        background = bool(background)
    # Si background y limit 0 (full index ~6min) → no bloquear HTTP
    if background and int(body.get("limit", 0) or 0) == 0:
        # chequea si ya corre
        st = _rag.index_status()
        if st.get("running"):
            return {"ok": True, "background": True, "already_running": True, **st}
        res = _rag.index_graph_background(limit=0)
        return res
    try:
        import asyncio as _aio
        res = await _aio.to_thread(_rag.index_graph, int(body.get("limit", 0) or 0))
        if not res.get("ok"):
            raise HTTPException(status_code=502, detail=res.get("error", "index failed"))
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/rag/index-status")
async def rag_index_status():
    """Estado del indexing background + stats."""
    from src.core import rag as _rag
    return _rag.index_status()


@app.get("/api/rag/stats")
async def rag_stats():
    from src.core import rag as _rag
    return _rag.stats()


@app.get("/api/graph/node/{node_id}")
async def graph_node(node_id: int):
    from src.brain.link import brain_path
    db_path = brain_path() / "knowledge_graph.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="knowledge_graph.db not found")
    try:
        # NOTE: URI mode=ro falla en SQLite 3.50.4 Windows; solo SELECT así que ruta plana es segura.
        con = sqlite3.connect(str(db_path), timeout=5.0)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("SELECT id, title, category, source, importance, tags, content, content_hash, created_at, updated_at, access_count FROM nodes WHERE id=?", (node_id,))
        row = cur.fetchone()
        if not row:
            con.close()
            raise HTTPException(status_code=404, detail="node not found")
        # neighbors
        cur.execute("SELECT id, from_node, to_node, relation, weight, note FROM edges WHERE from_node=? OR to_node=? LIMIT 100", (node_id, node_id))
        edge_rows = cur.fetchall()
        neighbors = []
        for r in edge_rows:
            other = r["to_node"] if int(r["from_node"]) == node_id else r["from_node"]
            cur.execute("SELECT id, title, category FROM nodes WHERE id=?", (int(other),))
            nr = cur.fetchone()
            neighbors.append({
                "edge_id": r["id"], "relation": r["relation"], "weight": r["weight"],
                "other_id": int(other), "other_title": (nr["title"][:80] if nr and nr["title"] else f"#{other}"), "other_category": nr["category"] if nr else "general",
                "direction": "out" if int(r["from_node"]) == node_id else "in"
            })
        con.close()
        return JSONResponse({
            "id": row["id"], "title": row["title"], "category": row["category"], "source": row["source"], "importance": row["importance"],
            "tags": row["tags"], "content": row["content"], "content_hash": row["content_hash"], "created_at": row["created_at"], "updated_at": row["updated_at"], "access_count": row["access_count"],
            "neighbors": neighbors, "degree": len(neighbors)
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Sessions ----
@app.get("/api/sessions")
async def list_sessions(limit: int = 50):
    slog = _session_log()
    try:
        sessions = slog.list_sessions(limit=limit)
        # SessionLog.list_sessions returns list[Session] — serialize
        out = []
        for s in sessions:
            if hasattr(s, "__dict__"):
                d = {k: getattr(s, k) for k in dir(s) if not k.startswith("_")}
                # Filter to known fields
                out.append({k: d[k] for k in ["id", "project", "parent_id", "summary", "compact_count", "total_tokens", "created_at", "last_active"] if k in d})
            else:
                out.append(dict(s) if isinstance(s, dict) else {"id": str(s)})
        return {"sessions": out}
    except Exception as e:
        # Fallback if API differs
        logger.warning(f"list_sessions fallback: {e}")
        return {"sessions": [], "fallback_error": str(e)}


@app.post("/api/sessions")
async def create_session(request: Request):
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    project = body.get("project", "default") if isinstance(body, dict) else "default"
    parent_id = body.get("parent_id") if isinstance(body, dict) else None
    slog = _session_log()
    sess = slog.create_session(project=project, parent_id=parent_id)
    return {"id": sess.id, "project": sess.project, "parent_id": sess.parent_id if hasattr(sess, "parent_id") else None}


@app.get("/api/sessions/{session_id}/messages")
async def get_messages(session_id: str, limit: int = 100, offset: int = 0):
    slog = _session_log()
    msgs = slog.get_messages(session_id, limit=limit, offset=offset)
    # Serialize Message dataclass
    out = []
    for m in msgs:
        if hasattr(m, "role"):
            out.append({"role": m.role, "content": m.content, "tool_call_id": getattr(m, "tool_call_id", ""), "tool_name": getattr(m, "tool_name", ""), "tokens": getattr(m, "tokens", 0), "model": getattr(m, "model", ""), "created_at": getattr(m, "created_at", "")})
        else:
            out.append(dict(m) if isinstance(m, dict) else {"content": str(m)})
    return {"session_id": session_id, "messages": out, "count": len(out)}


@app.post("/api/sessions/{session_id}/messages")
async def append_message(session_id: str, request: Request):
    body = await request.json()
    role = body.get("role", "user")
    content = body.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    slog = _session_log()
    from src.core.session_log import Message
    msg = Message(role=role, content=content, tool_call_id=body.get("tool_call_id", ""), tool_name=body.get("tool_name", ""))
    mid = slog.append(session_id, msg)
    return {"id": mid, "session_id": session_id}


@app.get("/api/sessions/{session_id}/search")
async def search_session(session_id: str, q: str, limit: int = 20):
    slog = _session_log()
    try:
        results = slog.search(q, limit=limit)
        out = []
        for m in results:
            if hasattr(m, "role"):
                out.append({"role": m.role, "content": m.content})
            else:
                out.append(dict(m) if isinstance(m, dict) else {"content": str(m)})
        return {"query": q, "results": out}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Ciclo #113: autoaprendizaje (re-wiring P1, colmena #132) ----
@app.get("/api/learning/stats")
async def learning_stats():
    """Paso 1-3: telemetría + Thompson + grafo + SkillOpt en un solo vistazo."""
    from src.core.learning_service import get_learning_service
    try:
        return get_learning_service().stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/learning/record")
async def learning_record(request: Request):
    """Registro manual de un turno (en v2 este endpoint daba 500)."""
    body = await request.json()
    task = body.get("task") or ""
    if not task:
        raise HTTPException(status_code=400, detail="task required")
    from src.core.learning_service import get_learning_service
    try:
        return await get_learning_service().record_turn(
            task=task,
            output=body.get("outcome") or "",
            model=body.get("model") or "",
            success=bool(body.get("success", True)),
            session_id=body.get("session_id") or "",
            tokens=int(body.get("tokens", 0) or 0),
            latency_ms=int(body.get("latency_ms", 0) or 0),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/learning/recommend")
async def learning_recommend(task: str = ""):
    """Paso 2: recomendación del router Thompson para una tarea."""
    from src.core.learning_service import get_learning_service
    try:
        return get_learning_service().recommend(task)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/skill-opt/stats")
async def skill_opt_stats():
    from src.core.learning_service import get_learning_service
    return get_learning_service().skill_opt.stats()


@app.get("/api/skill-opt/heldout")
async def skill_opt_heldout():
    from src.core.learning_service import get_learning_service
    return {"cases": get_learning_service().skill_opt.heldout()}


@app.post("/api/skill-opt/heldout")
async def skill_opt_set_heldout(request: Request):
    body = await request.json()
    cases = body.get("cases") or []
    from src.core.learning_service import get_learning_service
    try:
        n = get_learning_service().skill_opt.set_heldout(cases)
        return {"heldout_cases": n}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/skill-opt/propose")
async def skill_opt_propose(request: Request):
    body = await request.json()
    desc = body.get("description") or ""
    if not desc:
        raise HTTPException(status_code=400, detail="description required")
    from src.core.learning_service import get_learning_service
    return get_learning_service().skill_opt.propose(
        description=desc,
        config_delta=body.get("config_delta") or {},
        change_id=body.get("change_id"),
    )


@app.post("/api/skill-opt/validate")
async def skill_opt_validate(request: Request):
    """Valida un cambio contra held-out usando el pipeline REAL del agente.

    Fail-closed: si el provider no responde, los casos fallan y el cambio se
    rechaza (no se persiste).
    """
    body = await request.json()
    change_id = body.get("change_id") or ""
    if not change_id:
        raise HTTPException(status_code=400, detail="change_id required")
    required = float(body.get("required_score", 1.0))
    cases = body.get("cases")  # opcional: casos ad-hoc
    from src.core.learning_service import get_learning_service
    slog = _session_log()

    async def _eval(case: dict) -> bool:
        try:
            sess = slog.create_session(project="skill-opt")
            r = await _run_agent_once(str(case.get("task", "")), sess.id)
            expect = str(case.get("expect_contains", ""))
            return bool(expect) and expect in (getattr(r, "final_output", "") or "")
        except Exception:
            return False

    try:
        return await get_learning_service().skill_opt.validate(
            change_id, _eval, required_score=required, cases=cases)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/dream/run")
async def dream_run():
    """Dispara dream_distill a demanda (el scheduler corre cada 24h)."""
    from src.core.learning_service import get_learning_service
    return await get_learning_service().run_dream()


# ---- FS (Scope-guarded) ----
def _get_scope():
    from src.core.config import load_config
    # Fase 8: delega en _build_scope (canonico) — antes habia dos copias de la
    # construccion de Scope que podian divergir.
    return _build_scope(load_config())

@app.get("/api/fs/list")
async def fs_list(path: str = "./workspace"):
    from src.core.scope import ScopeDenied
    scope = _get_scope()
    try:
        p = scope.check_path(path)
        if not p.exists():
            raise HTTPException(status_code=404, detail=f"no existe: {path}")
        if p.is_file():
            stat = p.stat()
            return {"path": path, "type": "file", "size": stat.st_size}
        entries = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                # deny check — skip denied files
                scope.check_path(str(child))
            except ScopeDenied:
                continue
            entries.append({
                "name": child.name,
                "path": str(child.relative_to(_PROJECT_ROOT)).replace("\\", "/"),
                "type": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else 0,
            })
        return {"path": path, "entries": entries}
    except ScopeDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/fs/read")
async def fs_read(path: str):
    from src.core.scope import ScopeDenied
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    scope = _get_scope()
    try:
        p = scope.check_path(path)
        if not p.exists() or not p.is_file():
            raise HTTPException(status_code=404, detail=f"archivo no encontrado: {path}")
        max_b = scope.cfg.effective_max_bytes()
        size = p.stat().st_size
        if size > max_b:
            raise HTTPException(status_code=413, detail=f"archivo excede límite {max_b} bytes (tiene {size})")
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"path": path, "content": content, "size": size}
    except ScopeDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.put("/api/fs/write")
async def fs_write(request: Request):
    from src.core.scope import ScopeDenied
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    path = (body.get("path") or "").strip()
    content = body.get("content", "")
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    if not isinstance(content, str):
        content = str(content)
    scope = _get_scope()
    try:
        p = scope.check_path(path)
        # ensure parent exists
        p.parent.mkdir(parents=True, exist_ok=True)
        # scope check for write (mutating)
        scope.check_tool("write_file", {"path": path, "content": content})
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "size": len(content.encode("utf-8"))}
    except ScopeDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Fase 8: blocklist unica para los dos caminos de exec (endpoint /api/exec y
# tool exec del AgentLoop). Antes eran dos copias divergentes y solo POSIX;
# aqui se anade la superficie Windows (el harness corre en Windows por defecto)
# y se evitan falsos positivos tipo "docker --format json".
_CMD_BLOCKLIST = (
    re.compile(r"rm\s+-rf\s+(/|~|\$home|\$env:userprofile)", re.I),
    re.compile(r"mkfs", re.I),
    re.compile(r":\(\)\s*\{", re.I),
    re.compile(r"\b(shutdown|reboot|deltree|diskpart|bcdedit)\b", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\breg\s+delete\b", re.I),
    re.compile(r"\brd\s+/s(\s+/q)?", re.I),
    re.compile(r"\brmdir\s+/s", re.I),
    re.compile(r"remove-item\b[^\n]*-recurse", re.I),
    re.compile(r"\bdel\s+/[fs]\s+/[sq]\b", re.I),
)


def _blocked_command(cmd: str) -> str | None:
    """Devuelve el patron bloqueado si el comando es destructivo, o None."""
    for pat in _CMD_BLOCKLIST:
        if pat.search(cmd or ""):
            return pat.pattern
    return None


@app.post("/api/exec")
async def exec_cmd(request: Request):
    from src.core.scope import ScopeDenied
    import subprocess, shlex
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")
    command = (body.get("command") or body.get("cmd") or "").strip()
    cwd = (body.get("cwd") or "./workspace").strip() or "./workspace"
    timeout_s = int(body.get("timeout", 15))
    timeout_s = max(1, min(60, timeout_s))
    if not command:
        raise HTTPException(status_code=400, detail="command required")
    _blk = _blocked_command(command)
    if _blk is not None:
        raise HTTPException(status_code=403, detail=f"comando bloqueado por política ({_blk})")
    scope = _get_scope()
    try:
        cwd_p = scope.check_path(cwd)
        if not cwd_p.exists():
            cwd_p.mkdir(parents=True, exist_ok=True)
        if not cwd_p.is_dir():
            raise HTTPException(status_code=400, detail="cwd debe ser directorio dentro de allowed_roots")
    except ScopeDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    try:
        # ejecutar con shell — cwd jailado
        proc = await asyncio.to_thread(
            subprocess.run, command, shell=True, cwd=str(cwd_p),
            capture_output=True, text=True, timeout=timeout_s
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        # truncar salida gigante (20k chars)
        if len(out) > 20000:
            out = out[:20000] + "\n... [truncado]"
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": out, "cwd": str(cwd_p.relative_to(_PROJECT_ROOT)).replace("\\","/")}
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"timeout {timeout_s}s")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Chat (AgentLoop) ----

@app.get("/api/slash")
async def slash_list():
    from src.core.slash_commands import registry as slash_reg
    return {"commands": [c.to_dict() for c in slash_reg.list()]}

@app.post("/api/slash")
async def slash_exec(request: Request):
    from src.core.slash_commands import registry as slash_reg
    body = await request.json()
    raw = body.get("command") or body.get("raw") or ""
    ctx = body.get("ctx") or {}
    res = await slash_reg.execute(raw, ctx)
    if res.data.get("action") == "set_permission":
        perm = res.data.get("permission")
        try:
            from src.core.config import load_config, save_config
            cfg = load_config()
            cfg.setdefault("scope", {})["permission"] = perm
            save_config(cfg)
        except Exception as e:
            res.message += f" (persist failed: {e})"
    if res.data.get("action") == "set_plan_mode":
        on = res.data.get("on")
        try:
            from src.core.config import load_config, save_config
            cfg = load_config()
            if on is True:
                cfg.setdefault("scope", {})["permission"] = "read_only"
            elif on is False:
                cfg.setdefault("scope", {})["permission"] = "workspace_write"
            if on is not None:
                save_config(cfg)
        except Exception:
            pass
    return res.to_dict()

@app.get("/api/todos")
async def todos_get(session_id: str = ""):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    from src.core.todo_store import TodoStore as _TS
    return _TS(db_path=_PROJECT_ROOT / "data" / "session.db").read(session_id)

@app.post("/api/todos")
async def todos_post(request: Request):
    body = await request.json()
    sid = body.get("session_id") or body.get("sessionId") or ""
    todos = body.get("todos")
    if not sid or todos is None:
        raise HTTPException(status_code=400, detail="session_id and todos required")
    from src.core.todo_store import TodoStore as _TS2
    try:
        return _TS2(db_path=_PROJECT_ROOT / "data" / "session.db").write(sid, todos)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/permission")
async def permission_get():
    from src.core.config import load_config
    cfg = load_config()
    perm = cfg.get("scope", {}).get("permission", "workspace_write")
    return {"permission": perm, "options": ["read_only", "workspace_write", "full"]}

@app.put("/api/permission")
async def permission_put(request: Request):
    body = await request.json()
    perm = (body.get("permission") or "").lower()
    if perm not in ("read_only", "workspace_write", "full"):
        raise HTTPException(status_code=400, detail="permission must be read_only|workspace_write|full")
    from src.core.config import load_config, save_config
    cfg = load_config()
    cfg.setdefault("scope", {})["permission"] = perm
    save_config(cfg)
    return {"ok": True, "permission": perm}

@app.get("/api/plan")
async def plan_get():
    from src.core.config import load_config
    cfg = load_config()
    perm = cfg.get("scope", {}).get("permission", "workspace_write")
    return {"active": perm == "read_only", "permission": perm}


# ==== DECK: adjuntos de chat (Ctrl+V, capturas, archivos) ====
_UPLOADS_MAX_BYTES = 20 * 1024 * 1024
_UPLOAD_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_MAX_ATTACHMENTS = 8
_MAX_IMAGE_BYTES = 12 * 1024 * 1024
_MAX_TEXT_BYTES = 512 * 1024
_MAX_TEXT_CHARS = 24000
_TEXT_EXTS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".vue", ".html", ".css", ".sh", ".ps1",
    ".bat", ".sql", ".xml", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".cs",
    ".rb", ".php", ".log", ".svg",
}


def _uploads_dir() -> Path:
    d = _PROJECT_ROOT / "data" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name: str) -> str:
    name = (name or "archivo").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:120].strip()
    return name or "archivo"


def _upload_path(file_id: str) -> Path | None:
    """Resuelve data/uploads/{file_id}_* validando el id (regex estricta)."""
    if not _UPLOAD_ID_RE.match(file_id):
        return None
    matches = sorted(_uploads_dir().glob(f"{file_id}_*"))
    return matches[0] if matches else None


@app.post("/api/upload")
async def upload_post(file: UploadFile = File(...)):
    """Sube un adjunto (imagen o archivo) -> {id, url, name, mime, is_image, size}."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="archivo vacio")
    if len(data) > _UPLOADS_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"archivo muy grande (max {_UPLOADS_MAX_BYTES} bytes)")
    import mimetypes as _mt
    from uuid import uuid4
    fid = uuid4().hex[:8]
    fname = _safe_filename(file.filename or "archivo")
    (_uploads_dir() / f"{fid}_{fname}").write_bytes(data)
    mime = file.content_type or _mt.guess_type(fname)[0] or "application/octet-stream"
    return {
        "id": fid,
        "url": f"/api/uploads/{fid}",
        "name": fname,
        "mime": mime,
        "is_image": mime.startswith("image/"),
        "size": len(data),
    }


@app.get("/api/uploads/{file_id}")
async def upload_get(file_id: str):
    """Sirve un adjunto subido (id validado contra traversal)."""
    path = _upload_path(file_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="archivo no encontrado")
    import mimetypes as _mt
    mime = _mt.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=mime, filename=path.name.split("_", 1)[-1])


def _materialize_attachments(attachments: list) -> tuple[list[str], str]:
    """Adjuntos referenciados por id -> (imagenes data-URI, contexto textual).
    Se ejecuta en thread (lectura de disco)."""
    import base64 as _b64
    import mimetypes as _mt
    images: list[str] = []
    texts: list[str] = []
    for att in attachments[:_MAX_ATTACHMENTS]:
        if not isinstance(att, dict):
            continue
        path = _upload_path(str(att.get("id") or ""))
        if path is None or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except Exception:
            continue
        name = str(att.get("name") or path.name.split("_", 1)[-1])
        mime = str(att.get("mime") or _mt.guess_type(path.name)[0] or "application/octet-stream")
        ext = Path(name).suffix.lower()
        if mime.startswith("image/") and mime != "image/svg+xml":
            if len(data) <= _MAX_IMAGE_BYTES:
                images.append(f"data:{mime};base64,{_b64.b64encode(data).decode('ascii')}")
            else:
                texts.append(f"[imagen omitida por tamano: {name} ({len(data)} bytes)]")
        elif (
            mime.startswith("text/")
            or mime in ("application/json", "application/xml", "application/javascript",
                        "application/yaml", "application/x-yaml", "application/toml", "application/sql")
            or ext in _TEXT_EXTS
        ):
            if len(data) <= _MAX_TEXT_BYTES:
                txt = data.decode("utf-8", errors="replace")[:_MAX_TEXT_CHARS]
                texts.append(f"[archivo: {name} ({mime}, {len(data)} bytes)]\n{txt}")
            else:
                texts.append(f"[archivo adjunto grande (solo metadata): {name} ({mime}, {len(data)} bytes)]")
        else:
            texts.append(f"[archivo adjunto: {name} ({mime}, {len(data)} bytes)]")
    return images, "\n\n".join(texts)


# ==== DECK: voz — STT (faster-whisper local) + TTS (Piper local) ====
_AUDIO_SUFFIX = {
    "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/aac": ".m4a",
}


@app.get("/api/voice/status")
async def voice_status():
    """Estado de STT/TTS locales (init en thread: la 1ra llamada puede descargar modelos)."""
    def _status():
        from src.core.voice_engine import get_engine
        return get_engine().get_status()
    try:
        return await asyncio.to_thread(_status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/voice/stt")
async def voice_stt(request: Request):
    """Transcribe audio crudo del navegador (webm/ogg/wav) -> {text, ...}."""
    audio = await request.body()
    if not audio:
        raise HTTPException(status_code=400, detail="audio required")
    ct = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    suffix = _AUDIO_SUFFIX.get(ct, ".webm")
    language = request.query_params.get("language", "es")

    def _job():
        import tempfile as _tf
        from src.core.voice_engine import get_engine
        engine = get_engine()
        if not engine.stt_available:
            return {"error": "STT no disponible — faltan modelos de whisper (1ra ejecucion los descarga)"}
        with _tf.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            p = tmp.name
        try:
            return engine.transcribe_file(p, language=language)
        finally:
            try:
                os.unlink(p)
            except Exception:
                pass

    try:
        result = await asyncio.to_thread(_job)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"stt failed: {e}")
    if result.get("error"):
        raise HTTPException(status_code=501, detail=result["error"])
    return result


@app.post("/api/voice/tts")
async def voice_tts(request: Request):
    """Sintetiza voz (Piper local) -> WAV bytes."""
    body = await request.json()
    text = str(body.get("text") or "").strip()[:4000]
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    def _job():
        from src.core.voice_engine import get_engine
        engine = get_engine()
        if not engine.available:
            return None
        return engine.speak_bytes(text)

    try:
        wav = await asyncio.to_thread(_job)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"tts failed: {e}")
    if not wav:
        raise HTTPException(status_code=501, detail="TTS no disponible — faltan modelos Piper (voice_models/piper)")
    return Response(content=wav, media_type="audio/wav")


# ==== DECK: API keys BYOK (catalogo de proveedores) — .env en la raiz ====
_ENV_FILE = _PROJECT_ROOT / ".env"
_ENV_NAME_RE = re.compile(r"^[A-Z0-9_]{2,64}$")


def _read_env_file() -> dict[str, str]:
    vals: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _write_env_key(key: str, value: str) -> None:
    """Upsert (o borra si value vacio) de KEY=value en el .env raiz."""
    lines = _ENV_FILE.read_text(encoding="utf-8-sig").splitlines() if _ENV_FILE.exists() else []
    out = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    if value:
        if out and out[-1].strip():
            out.append("")
        out.append(f"{key}={value}")
        _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    elif _ENV_FILE.exists():
        # borrado: reescribir sin la linea; si no queda nada, eliminar el archivo
        if out:
            _ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
        else:
            try:
                _ENV_FILE.unlink()
            except Exception:
                pass


def _mask_key(v: str) -> str:
    if not v:
        return ""
    return (v[:4] + "…" + v[-4:]) if len(v) > 10 else "••••"


@app.get("/api/keys")
async def keys_get():
    """Estado de las API keys del catalogo (valores enmascarados, nunca el valor crudo)."""
    from src.core.provider_catalog import PROVIDER_CATALOG
    env_file = _read_env_file()
    seen: set[str] = set()
    keys = []
    for p in PROVIDER_CATALOG:
        env_name = p.get("api_key_env") or ""
        if not env_name or env_name in seen:
            continue
        seen.add(env_name)
        raw = os.environ.get(env_name) or env_file.get(env_name) or ""
        keys.append({
            "env": env_name,
            "label": p.get("name", env_name),
            "providers": [q["id"] for q in PROVIDER_CATALOG if q.get("api_key_env") == env_name],
            "configured": bool(raw),
            "source": "env" if os.environ.get(env_name) else ("file" if env_file.get(env_name) else ""),
            "masked": _mask_key(raw),
        })
    return {"keys": keys, "env_file": _ENV_FILE.name}


@app.put("/api/keys")
async def keys_put(request: Request):
    """Guarda/borra una API key en .env (raiz) + os.environ (efectivo al instante)."""
    body = await request.json()
    env_name = str(body.get("env") or "")
    value = str(body.get("value") or "").strip()
    if not _ENV_NAME_RE.match(env_name):
        raise HTTPException(status_code=400, detail="nombre de variable invalido")
    from src.core.provider_catalog import PROVIDER_CATALOG
    valid = {p.get("api_key_env") for p in PROVIDER_CATALOG if p.get("api_key_env")}
    if env_name not in valid:
        raise HTTPException(status_code=400, detail=f"variable desconocida: {env_name}")
    if value:
        _write_env_key(env_name, value)
        os.environ[env_name] = value
    else:
        _write_env_key(env_name, "")
        os.environ.pop(env_name, None)
    return {"ok": True, "env": env_name, "configured": bool(value), "masked": _mask_key(value) if value else ""}

# ---- Construccion de harness (compartida: /api/chat, /v1, /ws, /stream) ----
# Fase 8: antes /v1/chat/completions, /ws/chat y /api/chat/stream construian su
# propio loop con APIs inexistentes (ProviderRouter.from_config, Scope(permission=),
# loop.run() sin session_id, result.get() sobre dataclass) y siempre terminaban
# en error/echo. Ahora todos usan esta via — la misma que /api/chat ya probaba.

def _build_router(cfg: dict):
    """Router de providers desde config.

    Fase 8: delega en _build_router_from_config (ya existia y además adapta la
    lista de modelos Ollama a los realmente disponibles) — no duplicar wiring.
    """
    return _build_router_from_config(cfg)


def _full_access_roots() -> tuple[str, ...]:
    """Raices para permission=full: todas las unidades existentes (Windows) o / (POSIX).
    Sin letras de unidad hardcodeadas — se detectan en runtime."""
    roots: list[str] = []
    if sys.platform == "win32":
        import string as _string
        for letter in _string.ascii_uppercase:
            drive = f"{letter}:\\"
            try:
                if Path(drive).exists():
                    roots.append(drive)
            except Exception:
                continue
    else:
        roots.append("/")
    return tuple(roots) or ("/",)


def _build_scope(cfg: dict):
    """Scope jail desde config.scope (allowed_roots + deny_patterns + permission).

    Fase DECK: antes no se pasaba permission= y ScopeConfig quedaba en el default
    workspace_write -> 'full' no liberaba nada. Con permission=full se anaden
    todas las unidades y la red.
    """
    from src.core.scope import Scope, ScopeConfig
    scope_cfg = cfg.get("scope", {})
    permission = str(scope_cfg.get("permission", "workspace_write"))
    roots = tuple(scope_cfg.get("allowed_roots", ["./workspace", "./data"]))
    allow_net = bool(scope_cfg.get("allow_network", False))
    if permission == "full":
        roots = roots + tuple(r for r in _full_access_roots() if r not in roots)
        allow_net = True
    return Scope(ScopeConfig(
        allowed_roots=roots,
        deny_patterns=tuple(scope_cfg.get("deny_patterns", ["**/.env"])),
        max_read_bytes=int(scope_cfg.get("max_read_bytes", 1_048_576)),
        allow_network=allow_net,
        allow_mutating_without_confirm=bool(scope_cfg.get("allow_mutating_without_confirm", False)),
        permission=permission,
    ), project_root=_PROJECT_ROOT)


def _build_registry(scope, session_id: str):
    """Toolset estandar del harness, jail-ado por scope (closures que usan scope/session_id)."""
    from src.core.tool_registry import ToolRegistry
    registry = ToolRegistry()
    # --- Real tools for AgentLoop (Scope-guarded, no mock) ---
    def echo_tool(text: str) -> str:
        return text
    registry.register_function(echo_tool, name="echo", description="Echo text back (debug)", category="local")

    def read_file_tool(path: str) -> str:
        """Lee un archivo dentro de allowed_roots (./workspace, ./data)."""
        import pathlib
        p = scope.check_path(path)
        if not p.is_file():
            raise FileNotFoundError(f"no existe o no es archivo: {path}")
        size = p.stat().st_size
        if size > scope.cfg.effective_max_bytes():
            raise ValueError(f"archivo excede limite {scope.cfg.effective_max_bytes()} bytes (tiene {size})")
        return p.read_text(encoding="utf-8", errors="replace")

    def write_file_tool(path: str, content: str) -> str:
        """Escribe un archivo dentro de allowed_roots (crea directorios intermedios)."""
        p = scope.check_path(path)
        # scope guard for mutating op
        scope.check_tool("write_file", {"path": path, "content": content})
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"escrito {path} ({len(content.encode('utf-8'))} bytes)"

    def list_dir_tool(path: str = "./workspace") -> str:
        """Lista un directorio dentro de allowed_roots. Devuelve JSON."""
        import json as _json
        p = scope.check_path(path or "./workspace")
        if not p.exists():
            raise FileNotFoundError(f"no existe: {path}")
        if p.is_file():
            return _json.dumps({"path": path, "type": "file", "size": p.stat().st_size}, ensure_ascii=False)
        entries = []
        for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                scope.check_path(str(child))
            except Exception:
                continue
            entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file", "path": str(child.relative_to(_PROJECT_ROOT)).replace("\\", "/")})
        return _json.dumps({"path": path, "entries": entries}, ensure_ascii=False, indent=2)

    def exec_tool(command: str, cwd: str = "./workspace", timeout: int = 15) -> str:
        """Ejecuta un shell command jail-ado en cwd (strip + Scope). Usa la blocklist unica. Devuelve stdout+stderr."""
        import subprocess as _sp
        cmd = (command or "").strip()
        if not cmd:
            raise ValueError("command requerido")
        _blk = _blocked_command(cmd)
        if _blk is not None:
            raise PermissionError(f"comando bloqueado por politica ({_blk})")
        cwd_p = scope.check_path(cwd or "./workspace")
        if not cwd_p.exists():
            cwd_p.mkdir(parents=True, exist_ok=True)
        if not cwd_p.is_dir():
            raise NotADirectoryError("cwd debe ser directorio dentro de allowed_roots")
        timeout = max(1, min(60, int(timeout)))
        proc = _sp.run(cmd, shell=True, cwd=str(cwd_p), capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "") + (proc.stderr or "")
        if len(out) > 20000:
            out = out[:20000] + "\n... [truncado]"
        if proc.returncode != 0 and not out.strip():
            out = f"[exit {proc.returncode}]"
        return out or f"[exit {proc.returncode} sin salida]"

    registry.register_function(read_file_tool, name="read_file", description="Lee un archivo dentro de ./workspace o ./data (Scope jail)", category="local")
    registry.register_function(write_file_tool, name="write_file", description="Escribe un archivo dentro de ./workspace o ./data (crea dirs)", category="local")
    registry.register_function(list_dir_tool, name="list_dir", description="Lista un directorio dentro de allowed_roots", category="local")
    registry.register_function(exec_tool, name="exec", description="Ejecuta un comando shell jail-ado en ./workspace (bloquea destructivos)", category="local")

    # --- todo_write (harness parity whole-list replacement) ---
    _todo_store = TodoStore(db_path=_PROJECT_ROOT / "data" / "session.db", allow_parallel=True)
    def todo_write(todos):
        """Record and update a structured task list. Send ENTIRE list every call — it REPLACES previous. statuses pending|in_progress|completed."""
        if not isinstance(todos, list):
            raise ValueError("todos must be array")
        res = _todo_store.write(session_id, todos)
        return _json2.dumps(res, ensure_ascii=False)
    registry.register_function(todo_write, name="todo_write", description="Record and update a structured task list. Send ENTIRE list every call — it REPLACES previous. statuses pending|in_progress|completed", category="local", parameters={"type":"object","properties":{"todos":{"type":"array","description":"The COMPLETE task list, replacing any previous list.","items":{"type":"object","additionalProperties":False,"properties":{"content":{"type":"string","description":"What the task is — short imperative line."},"status":{"type":"string","enum":["pending","in_progress","completed"],"description":"pending|in_progress|completed"}}}}},"required":["todos"],"additionalProperties":False})

    def web_fetch(url: str, timeout: int = 12) -> str:
        """Fetch URL and return text (Scope network gate applies). Truncates to 15k."""
        scope.check_tool("web_fetch", {"url": url})
        import urllib.request, re
        timeout = max(2, min(30, int(timeout)))
        req = urllib.request.Request(url, headers={"User-Agent": "Nexus-v3/1.0 web_fetch"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read(15000)
                ctype = r.headers.get("content-type","")
                text = data.decode("utf-8", errors="replace")
                if "text/html" in ctype:
                    text = re.sub(r"<[^>]+>", " ", text)
                    text = re.sub(r"\s+", " ", text)
                return text[:15000]
        except Exception as e:
            raise RuntimeError(f"web_fetch failed: {e}")
    registry.register_function(web_fetch, name="web_fetch", description="Fetch URL and return text (network gate, truncates 15k)", category="local")
    return registry


async def _learning_record(task: str, session_id: str, result,
                           hint: str | None = None, model: str | None = None) -> None:
    """Paso 1-2 del ciclo #113: telemetria + judge + Thompson por turno.

    Fire-and-forget: el aprendizaje NUNCA puede romper una respuesta de chat.
    """
    try:
        from src.core.learning_service import get_learning_service
        await get_learning_service().record_turn(
            task=task,
            output=getattr(result, "final_output", "") or "",
            model=model or hint or "unknown",
            success=bool(getattr(result, "success", False)),
            session_id=session_id,
            tokens=int(getattr(result, "total_tokens", 0) or 0),
            latency_ms=int(getattr(result, "duration_ms", 0) or 0),
        )
    except Exception as e:
        logger.debug("learning record skipped: %s", e)


async def _run_agent_once(message: str, session_id: str, hint: str | None = None,
                          model: str | None = None, force_tools: bool = False):
    """Un run de AgentLoop con la construccion estandar (config segura)."""
    from src.core.config import load_config
    from src.core.agent_loop import AgentLoop
    cfg = _load_config_safe()
    router = _build_router(cfg)
    scope = _build_scope(cfg)
    registry = _build_registry(scope, session_id)
    loop = AgentLoop(provider_router=router, tool_registry=registry, scope=scope, session_log=_session_log())
    result = await loop.run(task=message, session_id=session_id, hint=hint,
                            force_tools=force_tools, model=model)
    await _learning_record(message, session_id, result, hint=hint, model=model)
    return result


@app.post("/api/chat")

async def chat(request: Request):
    """
    Body: { message: str, session_id?: str, hint?: str, model?: str }
    Runs one AgentLoop iteration. If providers unavailable, echoes with diagnostics.
    """
    body = await request.json()
    message = body.get("message") or body.get("content") or ""
    attachments = body.get("attachments") or []
    if not message and not attachments:
        raise HTTPException(status_code=400, detail="message required")
    session_id = body.get("session_id")
    hint = body.get("hint")
    # DECK: materializar adjuntos -> imagenes data-URI (vision) + contexto textual
    images: list[str] = []
    if attachments:
        try:
            images, attach_ctx = await asyncio.to_thread(_materialize_attachments, attachments)
            if attach_ctx:
                message = f"{message}\n\n{attach_ctx}" if message else attach_ctx
        except Exception as e:
            logger.warning(f"adjuntos ignorados: {e}")
    if not message:
        message = "[contenido adjunto sin texto]"
    # Inferir hint desde el mensaje si la UI no lo envía (enruta a la gema correcta).
    if not hint:
        from src.core.provider_router import infer_hint
        hint = infer_hint(message)
    # Ensure session
    slog = _session_log()
    if not session_id:
        sess = slog.create_session(project="default")
        session_id = sess.id
    else:
        # Ensure exists: get_messages devuelve [] para sesión inexistente (NO lanza),
        # así que se verifica con get_session para evitar IntegrityError (FK) al append.
        try:
            if slog.get_session(session_id) is None:
                # Crear con el id del cliente => estable entre turnos (memoria multi-turno).
                sess = slog.create_session(project="default", session_id=session_id)
        except Exception:
            sess = slog.create_session(project="default")
            session_id = sess.id

    # El mensaje del usuario y la respuesta del asistente los persiste el AgentLoop
    # (session_log=slog), para que el historial se cargue entre turnos (memoria multi-turno).
    # Try to run AgentLoop if providers are configured
    try:
        from src.core.config import load_config
        from src.core.agent_loop import AgentLoop

        cfg = load_config()
        router = _build_router(cfg)

        # Quick health probe with short timeout — don't block chat if providers offline
        health = {}
        if hasattr(router, "health_all"):
            try:
                health = await asyncio.wait_for(router.health_all(), timeout=3.0)
            except Exception:
                health = {}
        any_healthy = any(health.values()) if isinstance(health, dict) else False
        # If no provider resolves at all (no models registered), go fallback immediately
        # Otherwise attempt AgentLoop even if health check failed — loop will error and fallback will handle
        if not router.resolve(task_hint=hint or message) and not any_healthy:
            raise RuntimeError("no healthy provider — echo fallback")

        # Scope + tools (mismo helper que /v1, /ws y /stream)
        scope = _build_scope(cfg)
        registry = _build_registry(scope, session_id)


        loop = AgentLoop(provider_router=router, tool_registry=registry, scope=scope, session_log=slog)
        result = await loop.run(task=message, session_id=session_id, hint=hint,
                                force_tools=bool(body.get("force_tools", False)),
                                model=body.get("model"),
                                images=images or None)

        # Ciclo #113: telemetria + judge + Thompson por turno (nunca rompe chat)
        await _learning_record(message, session_id, result, hint=hint,
                               model=body.get("model"))

        # El asistente ya lo persiste el AgentLoop (session_log=slog). No duplicar.

        try:
            _t = TodoStore(db_path=_PROJECT_ROOT / "data" / "session.db").read(session_id)
        except Exception:
            _t = {"todos": [], "counts": {"pending":0,"inProgress":0,"completed":0}}
        perm_snap = cfg.get("scope", {}).get("permission", "workspace_write") if "cfg" in locals() else "workspace_write"
        return {
            "session_id": session_id,
            "reply": result.final_output,
            "todos": _t,
            "permission": perm_snap,
            "success": result.success,
            "iterations": result.iterations,
            "steps": [
                {"iteration": s.iteration, "phase": s.phase, "content": s.content, "tool_used": s.tool_used}
                for s in result.steps
            ],
            "mode": "agent_loop",
        }
    except Exception as e:
        logger.warning(f"chat fallback (echo): {e}", exc_info=True)
        # Echo fallback — still persist so UI works even without models
        from src.core.session_log import Message
        fallback = f"[offline echo] Recibido: {message}\n\n(Providers no disponibles o error: {e}. El arnés está en modo offline. Configura Ollama :11434 y reintenta.)"
        slog.append(session_id, Message(role="assistant", content=fallback))
        return {"session_id": session_id, "reply": fallback, "success": True, "mode": "echo_fallback", "error": str(e)}


# ---- Static SPA ----
# In production, Electron will serve ui/dist via this FastAPI static mount.
# This must be mounted AFTER /api routes to avoid shadowing.
_UI_DIST = _PROJECT_ROOT / "ui" / "dist"
if _UI_DIST.exists():
    # Serve assets at /assets
    assets_dir = _UI_DIST / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    async def _spa_root():
        return FileResponse(str(_UI_DIST / "index.html"))

    
# OPENAI
@app.post("/v1/chat/completions")
async def openai_chat_completions(req: Request):
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    messages = body.get("messages") or []
    user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                user_text = " ".join(str(p.get("text","")) for p in c if isinstance(p, dict))
            else:
                user_text = str(c or "")
            break
    if not user_text and messages:
        c = messages[-1].get("content")
        user_text = " ".join(str(p.get("text","")) for p in c if isinstance(p, dict)) if isinstance(c, list) else str(c or "")
    model = body.get("model") or "auto"
    stream = bool(body.get("stream"))
    try:
        # Fase 8: esta via estaba rota (from_config inexistente, Scope(permission=),
        # run() sin session_id, result.get() sobre dataclass) y devolvia "Error: ..."
        # siempre. Ahora reutiliza los helpers estandar + sesion por request.
        from src.core.provider_router import infer_hint
        slog = _session_log()
        sess = slog.create_session(project="openai-compat")
        result = await _run_agent_once(
            user_text,
            session_id=sess.id,
            hint=infer_hint(user_text),
            model=None if model == "auto" else model,
        )
        reply = result.final_output or ("[sin respuesta]" if result.success else "[sin respuesta final del modelo]")
    except Exception as e:
        import logging as _lg
        _lg.getLogger("nexus-v3").exception("openai compat error: %s", e)
        reply = f"Error: {e}"
    model_id = model if model != "auto" else "nexus-v3"
    if not stream:
        return JSONResponse({"id":"chatcmpl-nexus","object":"chat.completion","created":0,"model":model_id,"choices":[{"index":0,"message":{"role":"assistant","content": reply},"finish_reason":"stop"}],"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}})
    async def gen():
        import json as _js
        chunk = {"id":"chatcmpl-nexus","object":"chat.completion.chunk","created":0,"model":model_id,"choices":[{"index":0,"delta":{"content": reply},"finish_reason": None}]}
        yield f"data: {_js.dumps(chunk, ensure_ascii=False)}\n\n"
        done = {"id":"chatcmpl-nexus","object":"chat.completion.chunk","created":0,"model":model_id,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
        yield f"data: {_js.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
@app.get("/v1/models")
async def openai_models():
    cfg = _load_config_safe()
    try:
        # Fase 8: from_config no existe; listar via list_models() del router real.
        router = _build_router(cfg)
        models = [r.id for r in (router.list_models() or []) if getattr(r, "id", None)]
        if not models:
            models = ["auto", "nexus-v3"]
    except Exception:
        models = ["auto","nexus-v3"]
    return JSONResponse({"object":"list","data":[{"id":m,"object":"model","created":0,"owned_by":"nexus"} for m in models]})
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    # Fase 8: sesion por conexion (antes: run() sin session_id + APIs inexistentes).
    try:
        ws_session_id = _session_log().create_session(project="ws").id
    except Exception:
        ws_session_id = None
    try:
        while True:
            data = await ws.receive_text()
            try:
                import json as _js2
                payload = _js2.loads(data) if data.strip().startswith("{") else {"message": data}
                msg = payload.get("message") or payload.get("content") or data
                model = payload.get("model") or "auto"
            except Exception:
                msg, model = data, "auto"
            try:
                if not ws_session_id:
                    ws_session_id = _session_log().create_session(project="ws").id
                from src.core.provider_router import infer_hint
                result = await _run_agent_once(
                    msg,
                    session_id=ws_session_id,
                    hint=infer_hint(msg),
                    model=None if model == "auto" else model,
                )
                reply = result.final_output or ("[sin respuesta]" if result.success else "[sin respuesta final del modelo]")
            except Exception as e:
                reply = f"Error: {e}"
            await ws.send_text(reply)
    except WebSocketDisconnect:
        pass
    except Exception:
        try: await ws.close()
        except Exception: pass
@app.get("/api/chat/stream")
async def sse_chat(request: Request, message: str = "", model: str = "auto"):
    async def gen():
        import json as _js3
        try:
            # Fase 8: misma construccion estandar; sesion por request.
            from src.core.provider_router import infer_hint
            sess = _session_log().create_session(project="sse")
            result = await _run_agent_once(
                message,
                session_id=sess.id,
                hint=infer_hint(message),
                model=None if model == "auto" else model,
            )
            reply = result.final_output or ("[sin respuesta]" if result.success else "[sin respuesta final del modelo]")
        except Exception as e:
            reply = f"Error: {e}"
        yield f"data: {_js3.dumps({'reply': reply}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_fallback(full_path: str, request: Request):
        # Let /api already matched; this handles SPA routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        # Serve existing static file if present
        candidate = _UI_DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        # Fallback to index.html for client-side routing
        return FileResponse(str(_UI_DIST / "index.html"))


if not _UI_DIST.exists():
    @app.get("/", include_in_schema=False)
    async def _no_dist():
        return JSONResponse({"status": "ok", "dist": "ui/dist not built — run `npm run build` in ui/"})


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Nexus v3 harness backend")
    p.add_argument("--port", type=int, default=8765, help="Port to listen on (0=random)")
    p.add_argument("--host", type=str, default="127.0.0.1")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # If port 0, let uvicorn pick? Instead find free port ourselves
    if args.port == 0:
        import socket
        s = socket.socket()
        s.bind((args.host, 0))
        args.port = s.getsockname()[1]
        s.close()
        print(f"[nexus-v3] picked free port {args.port}")
    print(f"[nexus-v3] starting harness backend on {args.host}:{args.port}  (project root: {_PROJECT_ROOT})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
