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
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_credentials=False,
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
    # GET con efecto lateral (corre el agente) también pasa por el guard de origen.
    _mutating_get = request.method == "GET" and request.url.path.startswith("/api/chat/stream")
    if request.method in ("POST", "PUT", "PATCH", "DELETE") or _mutating_get:
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

# Referencias a tasks background (evita que el GC las recolecte a mitad de ejecucion).
_BG_TASKS: set = set()


def _spawn_bg(coro) -> None:
    """Lanza una coroutine en background reteniendo su referencia."""
    try:
        _t = asyncio.create_task(coro)
        _BG_TASKS.add(_t)
        _t.add_done_callback(_BG_TASKS.discard)
    except Exception:
        pass


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


def _event_store():
    if "event_store" not in _state:
        try:
            from src.core.event_store import EventStore
            from src.core.config import load_config
            cfg = load_config()
            db = cfg.get("session", {}).get("event_db_path", "./data/event_store.db")
            p = Path(db)
            if not p.is_absolute():
                p = _PROJECT_ROOT / p
            _state["event_store"] = EventStore(str(p))
        except Exception as e:
            logger.warning(f"EventStore init failed, using memory: {e}")
            from src.core.event_store import EventStore
            _state["event_store"] = EventStore(":memory:")
    return _state["event_store"]


def _agent_registry():
    if "agents" not in _state:
        from src.core.agent_registry import AgentRegistry
        _state["agents"] = AgentRegistry(_PROJECT_ROOT / "data" / "agents" / "registry.json")
    return _state["agents"]


def _assign_overrides(cfg: dict | None) -> dict:
    try:
        return ((cfg or {}).get("assignment", {}) or {}).get("overrides", {}) or {}
    except Exception:
        return {}


def _build_compaction(cfg: dict):
    from src.core.compaction import CompactionPipeline
    ccfg = dict((cfg or {}).get("compaction", {}) or {})
    budget = dict(ccfg.get("budget", {}) or {})
    pp = Path(budget.get("persist_dir", "./data/spill"))
    if not pp.is_absolute():
        pp = _PROJECT_ROOT / pp
    budget["persist_dir"] = str(pp)
    ccfg["budget"] = budget
    return CompactionPipeline(ccfg)


def _build_hooks(cfg: dict):
    from src.core.hooks_engine import HooksEngine, HookResult, HookAction
    hooks = HooksEngine((cfg or {}).get("hooks", {}) or {})
    if hooks.security_gate:
        try:
            from src.core.prompt_security import is_suspicious_content as _susp
        except Exception:
            _susp = None
        if _susp is not None:
            def _scan_tool(phase, payload):
                args = payload.get("args") or {}
                tool = payload.get("tool", "")
                for v in args.values():
                    try:
                        res = _susp(v) if isinstance(v, str) and v else None
                        if isinstance(res, dict) and res.get("is_suspicious"):
                            return HookResult(action=HookAction.STOP, message=f"entrada sospechosa bloqueada en {tool} (prompt_security)")
                    except Exception:
                        continue
                return HookResult(action=HookAction.CONTINUE)
            hooks.on("pre_tool_use", _scan_tool)
    return hooks


def _parse_json_items(text: str) -> list[dict]:
    """Extrae un array JSON de objetos {title, content, category, tags} de la respuesta del LLM."""
    import json as _j
    import re as _re
    if not text:
        return []
    m = _re.search(r"\[[\s\S]*\]", text)
    raw = m.group(0) if m else text
    try:
        data = _j.loads(raw)
    except Exception:
        return []
    out: list[dict] = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and str(it.get("content") or "").strip():
                tags = it.get("tags")
                out.append({
                    "title": str(it.get("title") or "")[:120],
                    "content": str(it["content"]).strip()[:1500],
                    "category": str(it.get("category") or "lesson").strip()[:40],
                    "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
                })
    return out


async def _distill_llm_extract(cfg: dict, digest: str, forced_model: str | None) -> list[dict]:
    """Extrae hechos/lecciones del digest con un modelo elegido POR CAPACIDAD (según lo instalado).

    Fiable: candidatos por prioridad, timeout por intento, validación JSON y fallback.
    """
    from src.core.provider_base import LLMMessage
    from src.core.model_assign import assign_model
    router = _get_router(cfg)
    cands: list[str] = []
    if forced_model:
        cands.append(forced_model)
    try:
        a = assign_model(router, model_hint="speed")
        if a.get("model"):
            cands.append(a["model"])
    except Exception:
        pass
    try:
        r = router.resolve()
        if r is not None:
            cands.append(r.model)
    except Exception:
        pass
    seen: set[str] = set()
    cands = [c for c in cands if c and not (c in seen or seen.add(c))]
    if not cands:
        return []
    prompt = ("Extrae del siguiente resumen de una sesión de trabajo los HECHOS y LECCIONES "
              "reutilizables para el futuro. Devuelve SOLO un array JSON de objetos con claves "
              "title, content, category, tags. Máximo 8. category en {lesson,concept,fix,config,decision}. "
              "content: 1-3 frases autosuficientes (sin 'él'/'eso'). Responde en español.")
    msgs = [LLMMessage(role="system", content=prompt), LLMMessage(role="user", content=digest[:6000])]
    for mdl in cands[:3]:
        try:
            resp = await asyncio.wait_for(router.chat(msgs, model=mdl), timeout=120)
            items = _parse_json_items(resp.content or "")
            if items:
                return items
        except Exception as e:
            logger.debug("distill llm modelo %s falló: %s", mdl, e)
            continue
    return []


def _wordset(s: str) -> set[str]:
    import re as _re
    return set(_re.findall(r"\w{4,}", (s or "").lower()))


def _near_dup(km, content: str, threshold: float = 0.5) -> bool:
    """True si el grafo ya tiene un nodo muy parecido (Jaccard de palabras >= umbral)."""
    ws = _wordset(content)
    if len(ws) < 3:
        return False
    # fts_search re-tokeniza y une con AND, asi que una query "w1 OR w2" nunca matchea.
    # Buscar una vez por palabra clave y unir los hits.
    hits: list = []
    for w in sorted(ws)[:8]:
        try:
            hits += km.fts_search(w, limit=6) or []
        except Exception:
            pass
    for hit in hits:
        hw = _wordset(hit.get("content") or hit.get("title") or "")
        if not hw:
            continue
        if len(ws & hw) / max(len(ws | hw), 1) >= threshold:
            return True
    return False


async def _distill_enrich(cfg: dict, session_id: str, forced_model: str | None, learn_fn) -> int:
    """Enriquecimiento LLM del digest (background-safe). Devuelve #items guardados (con dedup)."""
    from src.core.session_distill import build_session_digest
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        msgs = _session_log().get_messages(session_id, limit=800)
        dict_msgs = [{"role": m.role, "content": m.content, "tool_name": m.tool_name} for m in msgs]
        digest = build_session_digest(session_id, dict_msgs)["content"]
        items = await _distill_llm_extract(cfg, digest, forced_model)
        km = get_knowledge_memory()
        saved = 0
        for it in items:
            if _near_dup(km, it.get("content", "")):
                continue  # ya existe algo muy parecido -> no duplicar el grafo
            learn_fn(it["content"], topic_key="", category=it.get("category", "lesson"),
                     tags=it.get("tags", []), title=it.get("title", ""))
            saved += 1
        return saved
    except Exception as e:
        logger.debug("distill enrich falló: %s", e)
        return 0


async def _distill_run(cfg: dict, session_id: str, *, wait_llm: bool, llm_timeout: float | None = None) -> dict:
    """Destila una sesión: digest determinista (siempre) + enriquecimiento LLM por capacidad."""
    from src.core.session_distill import distill_session
    from src.core.knowledge_memory import get_knowledge_memory
    lc = (cfg or {}).get("learning", {}) or {}
    use_llm = bool(lc.get("distill_llm", True))
    forced_model = (lc.get("distill_model") or "").strip() or None
    msgs = _session_log().get_messages(session_id, limit=800)
    dict_msgs = [{"role": m.role, "content": m.content, "tool_name": m.tool_name} for m in msgs]
    km = get_knowledge_memory()

    def _learn(content, *, topic_key="", category="general", tags=None, title=""):
        return km.learn(content, topic_key=topic_key, category=category,
                        tags=tags or [], title=title, source="session-distill")

    res = distill_session(session_id, dict_msgs, _learn)  # determinista: siempre fiable
    if use_llm and res.get("turns"):
        if wait_llm:
            try:
                if llm_timeout:
                    res["llm_items"] = await asyncio.wait_for(
                        _distill_enrich(cfg, session_id, forced_model, _learn), timeout=llm_timeout)
                else:
                    res["llm_items"] = await _distill_enrich(cfg, session_id, forced_model, _learn)
            except asyncio.TimeoutError:
                # Determinista ya guardado; el LLM sigue en background (no re-correr todo).
                _spawn_bg(_distill_enrich(cfg, session_id, forced_model, _learn))
                res["llm_enrichment"] = "background(timeout)"
        else:
            _spawn_bg(_distill_enrich(cfg, session_id, forced_model, _learn))
            res["llm_enrichment"] = "background"
    logger.info("distill sesión %s -> %s piezas (%s turnos)", session_id, res.get("distilled"), res.get("turns"))
    return res


def _build_distill_hook(cfg: dict):
    """Hook pre-compactación (async): destila al grafo sin bloquear el turno.

    El digest determinista se guarda ya; el enriquecimiento LLM corre en background
    (nunca detiene el loop ni la compactación).
    """
    try:
        lc = (cfg or {}).get("learning", {}) or {}
    except Exception:
        lc = {}
    if not bool(lc.get("distill_on_compact", True)):
        return None

    async def _hook(session_id: str):
        try:
            return await _distill_run(cfg, session_id, wait_llm=False)
        except Exception as e:
            logger.debug("distill hook falló: %s", e)
    return _hook


def _session_distilled(session_id: str) -> bool:
    """True si la sesión ya tiene su nodo consolidado en el grafo."""
    try:
        from src.core.knowledge_memory import get_knowledge_memory
        conn = get_knowledge_memory()._connect()
        row = conn.execute("select 1 from nodes where topic_key=? limit 1",
                           (f"session/{session_id}",)).fetchone()
        return row is not None
    except Exception:
        return False


async def _distill_orphan_sessions(limit: int = 50) -> int:
    """Red de seguridad: destila sesiones con turnos que nunca compactaron (al arrancar)."""
    cfg = _state.get("config") or _load_config_safe()
    lc = (cfg or {}).get("learning", {}) or {}
    if not bool(lc.get("distill_on_session_end", True)):
        return 0
    n = 0
    try:
        for s in _session_log().list_sessions(limit=limit):
            sid = getattr(s, "id", None)
            if not sid or _session_distilled(sid):
                continue
            if not _session_log().get_messages(sid, limit=1):
                continue
            await _distill_run(cfg, sid, wait_llm=False)
            n += 1
            await asyncio.sleep(0.3)
    except Exception as e:
        logger.debug("distill orphan catch-up falló: %s", e)
    if n:
        logger.info("distill catch-up: %d sesiones destiladas al arrancar", n)
    return n


@app.on_event("startup")
async def _startup():
    logger.info("Starting Nexus v3 harness backend")
    _state["config"] = _load_config_safe()
    _state["brain"] = _brain_info_safe()
    # Concurrencia adaptativa: el agent loop lee NEXUS_PARALLEL_TOOLS (config concurrency.parallel_tools)
    try:
        import os as _os
        _cc = (_state["config"].get("concurrency", {}) or {})
        _os.environ["NEXUS_PARALLEL_TOOLS"] = "1" if _cc.get("parallel_tools", True) else "0"
    except Exception:
        pass
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
    # Red de seguridad: destilar sesiones con turnos que nunca compactaron (background)
    try:
        import asyncio as _asyncio_d
        _spawn_bg(_distill_orphan_sessions())
    except Exception:
        pass
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
        _spawn_bg(_bg_zen())
    except Exception:
        pass
    # Descubrimiento de modelos BYOK (/v1/models) en background — mantiene el
    # selector al dia sin bloquear el arranque.
    try:
        from src.core import provider_models as _pm_startup
        _spawn_bg(_pm_startup.refresh_all(force=False))
    except Exception:
        pass
    # LearningService (re-wiring ciclo #113, colmena #132): orden EXPLICITO
    # post-arranque, sin actor system. Nunca debe tumbar el startup.
    try:
        from src.core.learning_service import get_learning_service
        _ls = get_learning_service()

        async def _bg_learning():
            try:
                await _ls.start(dream_interval_s=86400.0, enable_dream=True)
                logger.info("LearningService started (judge+Thompson+grafo+dream scheduler)")
            except Exception as e:
                logger.warning(f"LearningService bg start failed: {e}")

        # No bloquear el arranque: /api/health debe responder ya. El servicio es
        # singleton y los requests lo obtienen lazy via get_learning_service().
        _spawn_bg(_bg_learning())
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
    try:
        # Cerrar el router AHORA (el cierre diferido no corre al parar el loop).
        r = _state.pop("router", None)
        _state.pop("router_sig", None)
        if r is not None:
            _close_router(r)
        for _rr in _RETIRED_ROUTERS:
            _close_router(_rr)
        _RETIRED_ROUTERS.clear()
    except Exception:
        pass
    # Drenar tasks background y de cierre diferido (evita fugas de sockets/FDs al salir).
    try:
        _all = list(_BG_TASKS) + list(_CLOSE_TASKS)
        for _t in _all:
            _t.cancel()
        if _all:
            await asyncio.gather(*_all, return_exceptions=True)
    except Exception:
        pass


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
        router.register_provider("ollama", OllamaProvider(
            base_url=_base, timeout_s=oc.get("timeout_s", 300),
            keep_alive=oc.get("keep_alive", "30m"),
            options=oc.get("options") or {},
        ))
        for m in _resolved:
            router.register_model(ModelRoute(id=m, provider_name="ollama"))
    if "llamacpp" in prov_cfg:
        lc = prov_cfg["llamacpp"]
        router.register_provider("llamacpp", LlamaCppProvider(
            base_url=lc.get("base_url", "http://localhost:8080"),
            fallback_url=lc.get("fallback_url"), timeout_s=lc.get("timeout_s", 300),
            max_tokens=lc.get("max_tokens", 2048), options=lc.get("options") or {},
        ))
        for m in lc.get("models", []) or []:
            router.register_model(ModelRoute(id=m, provider_name="llamacpp"))
    if "cloud" in prov_cfg:
        cc = prov_cfg["cloud"]
        if cc.get("enabled"):
            router.register_provider("cloud", CloudProvider(base_url=cc.get("base_url"), api_key=cc.get("api_key"), auth_style=cc.get("auth_style", "bearer"), strict_tools=cc.get("strict_tools", False)))
            for m in cc.get("models", []) or []:
                router.register_model(ModelRoute(id=m, provider_name="cloud"))
    # DECK: proveedores BYOK del catalogo — se registran solo con key en env/.env.
    # CloudProvider anade /v1/chat/completions al base_url, asi que se recorta el
    # sufijo /v1 del catalogo. Solo auth bearer (compat OpenAI); cohere/gemini/
    # anthropic tienen formatos distintos -> no se registran aqui.
    try:
        from src.core.provider_catalog import PROVIDER_CATALOG
        from src.core import provider_models as _pm
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
            # Registrar TODOS los modelos conocidos: cache en vivo (aunque este
            # vencida) o, si no hay, el catalogo estatico. Asi el routing cubre
            # los modelos que el proveedor expone hoy (el catalogo envejece).
            known = _pm.cached_models(pid, max_age_s=-1) or ((pc.get("models") or [pc["default_model"]]) + (pc.get("extra_models") or []))
            for mid in known:
                router.register_model(ModelRoute(id=mid, provider_name=pid))
            if pid not in router.fallback_order:
                router.fallback_order.append(pid)
            logger.info(f"BYOK catalogo activo: {pid} ({len(known)} modelos)")
    except Exception as e:
        logger.warning(f"catalogo BYOK no registrado: {e}")
    return router


async def _probe_providers(timeout_s: float = 2.5) -> dict:
    """Live health probe — returns {name: {ok, models, status, error}} without mutating global router."""
    cfg = _state.get("config") or _load_config_safe()
    router = None
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
    finally:
        # Cerrar el router temporal (evita fuga de httpx/FDs: /api/health se poléa)
        if router is not None:
            _close_router(router)


@app.get("/api/health")
async def health():
    cfg = _state.get("config") or _load_config_safe()
    brain = _state.get("brain") or _brain_info_safe()
    # Liveness rapida: NO sondear providers aqui. El sondeo tarda ~2s y hace que
    # el handshake de Electron (aborta a 2s) marque el backend como offline.
    # El inventario real vive en /api/providers; aqui se devuelve su ultimo cache.
    return {
        "status": "ok",
        "service": "nexus-v3-harness",
        "version": "3.0.0-alpha",
        "project": cfg.get("project") if isinstance(cfg, dict) else "nexus-v3",
        "valid": cfg.get("_validation_errors") == [] if isinstance(cfg, dict) else False,
        "validation_errors": cfg.get("_validation_errors") if isinstance(cfg, dict) else [],
        "brain": brain,
        "providers": _state.get("providers_cache", {}),
    }


@app.get("/api/providers")
async def providers_status(refresh: bool = False):
    """Dynamic provider inventory — only online providers report models. Used by ModelSelector.
    refresh=1 fuerza re-descubrir /v1/models de los proveedores BYOK (mantiene el selector al dia)."""
    cfg = _state.get("config") or _load_config_safe()
    if refresh:
        try:
            from src.core import provider_models as _pm
            await _pm.refresh_all(force=True)
        except Exception as e:
            logger.debug(f"refresh provider models failed: {e}")
    probe = await _probe_providers(timeout_s=3.0)
    _state["providers_cache"] = probe  # para que /api/health lo exponga sin sondear
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
    from src.core import provider_models as _pm2
    _byok = _pm2.byok_ids()
    for pname, info in probe.items():
        if pname == "error":
            continue
        if not info.get("ok"):
            continue  # offline -> don't expose models
        mids = list(info.get("registered") or [])
        if pname in _byok:
            # Proveedor BYOK: sumar la lista EN VIVO de /v1/models (siempre al dia,
            # el catalogo estatico envejece: ej. opencode-go cambia sus modelos).
            for m in info.get("models") or []:
                if _pm2.is_chat_model(m) and m not in mids:
                    mids.append(m)
        seen_ids: set[str] = set()
        for mid in mids:
            ctx = llamacpp_ctx if pname == "llamacpp" else 32000
            # embed model should not appear as chat model
            if "embed" in mid.lower() and "nomic" in mid.lower():
                continue
            short_id, label = _pretty_id(mid)
            # Dedup POR PROVEEDOR: cada proveedor muestra su lista completa aunque
            # un mismo modelo exista en otro (ej. opencode-go incluye el catalogo Zen).
            if short_id in seen_ids:
                continue
            seen_ids.add(short_id)
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
    """Config para UI. Nunca expone secretos: enmascara api_key resueltas."""
    import copy as _copy
    try:
        cfg = _copy.deepcopy(_state.get("config") or _load_config_safe())
    except Exception:
        cfg = dict(_state.get("config") or {})
    try:
        for _name, _p in (cfg.get("providers") or {}).items():
            if isinstance(_p, dict) and _p.get("api_key"):
                _p["api_key"] = _mask_api_key(str(_p.get("api_key")))
    except Exception:
        pass
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
            m["_has_env_key"] = raw_key.startswith("${")
            # Nunca devolver la clave en claro: solo la referencia ${ENV} o vacio.
            m["api_key"] = raw_key if raw_key.startswith("${") else ""
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
                if v is None:
                    v = ""  # None == vacio (no clobber con null)
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
        # Refresh cached config + invalidar router (providers cambiaron)
        _state["config"] = _load_config_safe()
        _invalidate_router()
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
    # Anti-SSRF mínimo: solo http(s) y bloquear metadata cloud (sin IPs hardcodeadas).
    # (No bloqueamos loopback/privado: el harness es local y prueba ollama/llamacpp locales.)
    for _u in (base_url, fallback_url):
        if not _u:
            continue
        if not _u.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"base_url debe ser http(s): {_u}")
        _host = (_urlparse(_u).hostname or "").lower()
        _is_meta = (_host.endswith("metadata.google.internal")
                    or _host.startswith("169.254.")      # link-local
                    or _host.startswith("100.100.100."))  # alibaba metadata
        if _is_meta:
            raise HTTPException(status_code=400, detail="base_url no permitida (metadata cloud)")
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
    # Semilla fija: comunidades reproducibles entre requests y sesiones.
    rng = random.Random(20260101)
    for _ in range(iters):
        order = node_ids[:]
        rng.shuffle(order)
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

def _load_graph_data(
    limit: int = 300,
    q: str = "",
    category: str = "",
    community: str = "",
    min_importance: int = 0,
    include_isolated: bool = False,
    relation: str = "",
    min_weight: float = 0.0,
    centrality: str = "hybrid",
) -> dict:
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
        total_edge_count = len(edge_rows)
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
            rel = r["relation"] or "relates_to"
            weight = float(r["weight"] or 0.5)
            if relation and relation != "all" and rel != relation:
                continue
            if weight < min_weight:
                continue
            raw_edges.append((u, v))
            edge_objs.append({"id": r["id"], "source": u, "target": v, "relation": rel, "weight": weight, "label": rel})
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
    PALETTE = ["#4C9DFF", "#3DDC84", "#FFC857", "#61C5D9", "#FF756D", "#A7B8CC", "#7AABFF", "#66C2A5", "#F2A65A", "#83B8FF", "#C5A3FF", "#6DD3CE"]
    CATEGORY_COLOR: dict[str,str] = {"general":"#788493","architecture":"#4C9DFF","concept":"#61C5D9","deploy":"#FFC857","fix":"#3DDC84","fact":"#F2A65A","lesson":"#A7B8CC"}

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
    if centrality == "degree":
        filtered.sort(key=lambda n: (n["degree"], n["pagerank"], n["importance"]), reverse=True)
    elif centrality == "pagerank":
        filtered.sort(key=lambda n: (n["pagerank"], n["degree"], n["importance"]), reverse=True)
    else:
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
        "total_edges": total_edge_count,
        "filtered_edges": len(edge_objs),
        "visible_nodes": len(visible_nodes),
        "visible_edges": len(visible_edges),
        "communities": len(set(comm_map.values())) if comm_map else 0,
        "avg_degree": round(sum(deg.values())/(2*len(all_node_ids)), 2) if all_node_ids else 0,
        "max_pagerank": max(pr_map.values()) if pr_map else 0,
        "categories": all_categories,
        "relations": sorted({str(r["relation"] or "relates_to") for r in edge_rows}),
        "centrality": centrality if centrality in ("hybrid", "degree", "pagerank") else "hybrid",
    }
    # community summary
    from collections import Counter
    comm_counter = Counter(comm_map.get(n["id"], 0) for n in visible_nodes)
    communities = [{"id": cid, "size": comm_counter.get(cid,0), "color": PALETTE[cid % len(PALETTE)], "label": f"Comunidad {cid}"} for cid in sorted(comm_counter.keys())]

    return {"nodes": visible_nodes, "edges": visible_edges, "stats": stats, "communities": communities}

@app.get("/api/graph")
@app.get("/api/knowledge/graph")
async def graph(
    limit: int = 300,
    q: str = "",
    search: str = "",
    category: str = "",
    community: str = "",
    min_importance: int = 0,
    include_isolated: str = "false",
    relation: str = "",
    min_weight: float = 0.0,
    centrality: str = "hybrid",
):
    inc = include_isolated.lower() in ("1","true","yes")
    # clamp limit
    limit = max(10, min(2000, limit))
    min_weight = max(0.0, min(1.0, min_weight))
    if centrality not in ("hybrid", "degree", "pagerank"):
        centrality = "hybrid"
    data = _load_graph_data(
        limit=limit, q=(q or search).strip(), category=category.strip(),
        community=community.strip(), min_importance=min_importance,
        include_isolated=inc, relation=relation.strip(), min_weight=min_weight,
        centrality=centrality,
    )
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
        # GET no debe mutar: se quitó el touch(node_id) — cross-site <img>/crawlers inflaban access_count.
        return JSONResponse({
            "id": row["id"], "title": row["title"], "category": row["category"], "source": row["source"], "importance": row["importance"],
            "tags": row["tags"], "content": row["content"], "content_hash": row["content_hash"], "created_at": row["created_at"], "updated_at": row["updated_at"], "access_count": row["access_count"],
            "neighbors": neighbors, "degree": len(neighbors)
        })
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Método OPCODE: vía única de escritura de conocimiento ----
@app.post("/api/learn/direct")
async def learn_direct(request: Request):
    """Vía ÚNICA de escritura de conocimiento (paridad OPCODE v2).

    Body: {content, topic?, category?, tags?, type?, importance?}
    -> 4 destinos: grafo(FTS5+entities+auto-edges) + RAG + markdown
       + observation (best-effort). topic -> UPSERT (revision_count++).
    """
    body = await _json_body(request)
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        res = await asyncio.to_thread(
            lambda: get_knowledge_memory().learn(
                content,
                topic_key=body.get("topic") or body.get("topic_key") or "",
                category=body.get("category") or "general",
                tags=body.get("tags") or [],
                title=body.get("title") or "",
                source=body.get("source") or "api",
                importance=int(body.get("importance") or 5),
            )
        )
        return JSONResponse(res)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph/status")
async def graph_status():
    """Estado del grafo OPCODE: nodos, aristas, topic/nodos, FTS, entidades."""
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        st = await asyncio.to_thread(lambda: get_knowledge_memory().status())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    try:
        from src.core import rag as _rag
        st["rag_chunks"] = _rag.stats().get("chunks", 0)
    except Exception:
        st["rag_chunks"] = -1
    return JSONResponse(st)


@app.get("/api/graph/search")
async def graph_search(q: str, limit: int = 20):
    """Búsqueda FTS5 (bm25) sobre los nodos del grafo."""
    if not q.strip():
        raise HTTPException(status_code=400, detail="q required")
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        results = await asyncio.to_thread(
            lambda: get_knowledge_memory().fts_search(
                q.strip(), max(1, min(100, limit))
            )
        )
        return {"query": q, "results": results, "count": len(results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph/entities")
async def graph_entities(limit: int = 30):
    """Top entidades extraídas (mención + nodos que las comparten)."""
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        ents = await asyncio.to_thread(
            lambda: get_knowledge_memory().get_entities(max(1, min(200, limit)))
        )
        return {"entities": ents, "count": len(ents)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph/related/{node_id}")
async def graph_related(node_id: int, limit: int = 30):
    """Vecinos de un nodo (paridad find_related v2)."""
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        res = await asyncio.to_thread(
            lambda: get_knowledge_memory().find_related(
                node_id, max(1, min(200, limit))
            )
        )
        if "error" in res:
            raise HTTPException(status_code=404, detail=res["error"])
        return JSONResponse(res)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph/path/{node_a}/{node_b}")
async def graph_path(node_a: int, node_b: int, max_depth: int = 4):
    """Ruta BFS mínima entre dos nodos (sin dirección, hasta 12 saltos)."""
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        return await asyncio.to_thread(
            lambda: get_knowledge_memory().find_path(
                node_a, node_b, max_depth=max(1, min(12, max_depth))
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/graph/maintain")
async def graph_maintain():
    """brain_guard-lite: dedupe hashes, self-loops, tope MAX_EDGES."""
    from src.core.knowledge_memory import get_knowledge_memory
    try:
        res = await asyncio.to_thread(lambda: get_knowledge_memory().maintain())
        return JSONResponse(res)
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
    body = await _json_body(request, required=False)
    project = body.get("project", "default") if isinstance(body, dict) else "default"
    parent_id = body.get("parent_id") if isinstance(body, dict) else None
    slog = _session_log()
    sess = slog.create_session(project=project, parent_id=parent_id)
    return {"id": sess.id, "project": sess.project, "parent_id": sess.parent_id if hasattr(sess, "parent_id") else None}


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    """Borra una sesión y sus mensajes (FK ON DELETE CASCADE + triggers FTS)."""
    slog = _session_log()
    try:
        deleted = slog.delete_session(session_id)
    except Exception as e:
        raise HTTPException(status_code=409,
                            detail=f"no se puede borrar {session_id}: {e} — borrá primero las sesiones hijas (fork/branch)")
    if not deleted:
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    return {"ok": True, "deleted": True, "id": session_id}


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
    body = await _json_body(request)
    role = body.get("role", "user")
    content = body.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    slog = _session_log()
    from src.core.session_log import Message
    msg = Message(role=role, content=content, tool_call_id=body.get("tool_call_id", ""), tool_name=body.get("tool_name", ""))
    mid = slog.append(session_id, msg)
    return {"id": mid, "session_id": session_id}


@app.get("/api/sessions/global/search")
async def search_global(q: str, limit: int = 20):
    """FTS5 GLOBAL (todas las sesiones). Debe declararse ANTES de /{session_id}/search."""
    slog = _session_log()
    try:
        results = slog.search(q, limit=limit)
        out = []
        for m in results:
            if hasattr(m, "role"):
                out.append({"role": m.role, "content": m.content})
            else:
                out.append(dict(m) if isinstance(m, dict) else {"content": str(m)})
        return {"query": q, "results": out, "count": len(out)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions/catalog")
async def sessions_catalog(limit: int = 200):
    """Catálogo de sesiones con conteo de mensajes."""
    slog = _session_log()
    try:
        out = []
        for s in slog.list_sessions(limit=limit):
            sid = getattr(s, "id", None)
            try:
                cnt = slog.count_messages(sid) if sid else 0
            except Exception:
                cnt = 0
            out.append({"id": sid, "project": getattr(s, "project", ""),
                        "summary": getattr(s, "summary", ""), "messages": cnt})
        return {"sessions": out, "count": len(out)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str):
    """Exporta el log de la sesión como ZIP (JSON adentro)."""
    import io as _io, zipfile as _zip, json as _json3
    slog = _session_log()
    try:
        msgs = slog.get_messages(session_id, limit=100000)
        data = [{"role": getattr(m, "role", ""), "content": getattr(m, "content", ""),
                 "created_at": getattr(m, "created_at", "")} for m in msgs]
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as z:
            z.writestr(f"{session_id}.json",
                       _json3.dumps({"session_id": session_id, "messages": data}, ensure_ascii=False, indent=2))
        buf.seek(0)
        return Response(content=buf.read(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{session_id}.zip"'})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/sessions/{session_id}/search")
async def search_session(session_id: str, q: str, limit: int = 20):
    slog = _session_log()
    try:
        results = slog.search_session(session_id, q, limit=limit)
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
    body = await _json_body(request)
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
    body = await _json_body(request)
    cases = body.get("cases") or []
    from src.core.learning_service import get_learning_service
    try:
        n = get_learning_service().skill_opt.set_heldout(cases)
        return {"heldout_cases": n}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/skill-opt/propose")
async def skill_opt_propose(request: Request):
    body = await _json_body(request)
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
    body = await _json_body(request)
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
def _rel(p) -> str:
    """Ruta relativa al proyecto (POSIX) o absoluta si está fuera (no lanza)."""
    try:
        return str(Path(p).relative_to(_PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        return str(p).replace("\\", "/")


def _get_scope():
    from src.core.config import load_config
    # Fase 8: delega en _build_scope (canonico) — antes habia dos copias de la
    # construccion de Scope que podian divergir.
    return _build_scope(load_config())

async def _json_body(request: Request, required: bool = True):
    """Parsea el body JSON del request. Body invalido -> 400 (no 500).
    required=False devuelve {} si el body falta o es invalido (endpoints opcionales)."""
    try:
        return await request.json()
    except Exception:
        if required:
            raise HTTPException(status_code=400, detail="invalid json")
        return {}


@app.get("/api/fs/list")
async def fs_list(path: str = "./workspace"):
    from src.core.scope import ScopeDenied
    scope = _get_scope()
    # Raiz vacia / "/" -> listar unidades (Windows: C:\, D:\ ...) o "/" en POSIX.
    if path in ("", "/", "\\"):
        import string as _string
        if os.name == "nt":
            drives = [f"{d}:\\" for d in _string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return {"path": "", "root": True,
                    "entries": [{"name": d, "path": d, "type": "dir", "size": 0} for d in drives]}
        path = "/"
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
                "path": _rel(child),
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
        # auth de escritura PRIMERO, luego crear directorios (no crear dirs si se deniega)
        scope.check_tool("write_file", {"path": path, "content": content})
        p.parent.mkdir(parents=True, exist_ok=True)
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


# Fase 11: jail real de exec (best-effort, no sandbox de SO). En modo no-'full'
# se prohibe encadenar/redirigir/sustituir (permite escapar a otro comando o a
# una ruta externa) y se valida que cada token tipo-ruta caiga dentro de
# allowed_roots resolviendo contra el cwd. Con permission='full' se permite
# shell completo (el usuario lo activa explicitamente en Ajustes -> Permisos).
_EXEC_META = re.compile(r"[;&|`<>]|\$\(|\$\{|\r|\n")
_EXEC_PATHY = re.compile(r"[\\/]|^[A-Za-z]:")


def _exec_jail(cmd: str, cwd_p, scope) -> str | None:
    """Motivo de bloqueo del comando, o None si esta permitido en el jail."""
    from src.core.scope import ScopeDenied
    perm = getattr(scope.cfg, "permission", "workspace_write") or "workspace_write"
    if perm == "full":
        return None
    m = _EXEC_META.search(cmd or "")
    if m:
        return (
            f"encadenamiento/redireccion/sustitucion bloqueado en modo jail ({m.group(0)!r}). "
            f"Activa Ajustes -> Permisos -> 'full' para shell completo."
        )
    try:
        import shlex
        toks = shlex.split(cmd or "", posix=False)
    except Exception:
        toks = (cmd or "").split()
    for raw in toks:
        t = raw.strip("\"'")
        if not t or t.startswith("-"):
            continue
        if not (_EXEC_PATHY.search(t) or t.startswith("..")):
            continue
        p = Path(t)
        if not p.is_absolute():
            p = cwd_p / p
        try:
            scope.check_path(p)
        except ScopeDenied:
            return (
                f"ruta fuera del jail: {t!r} (permitidas: {scope.cfg.allowed_roots}). "
                f"Activa 'full' para acceso total a la PC."
            )
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
    try:
        timeout_s = int(body.get("timeout", 15))
    except (TypeError, ValueError):
        timeout_s = 15
    timeout_s = max(1, min(60, timeout_s))
    if not command:
        raise HTTPException(status_code=400, detail="command required")
    _blk = _blocked_command(command)
    if _blk is not None:
        raise HTTPException(status_code=403, detail=f"comando bloqueado por política ({_blk})")
    scope = _get_scope()
    try:
        scope.check_tool("exec", {"command": command})
        cwd_p = scope.check_path(cwd)
        if not cwd_p.exists():
            cwd_p.mkdir(parents=True, exist_ok=True)
        if not cwd_p.is_dir():
            raise HTTPException(status_code=400, detail="cwd debe ser directorio dentro de allowed_roots")
    except ScopeDenied as e:
        raise HTTPException(status_code=403, detail=str(e))
    _jail = _exec_jail(command, cwd_p, scope)
    if _jail is not None:
        raise HTTPException(status_code=403, detail=_jail)
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
        return {"ok": proc.returncode == 0, "exit_code": proc.returncode, "output": out, "cwd": _rel(cwd_p)}
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
    body = await _json_body(request)
    raw = body.get("command") or body.get("raw") or ""
    ctx = body.get("ctx") or {}
    res = await slash_reg.execute(raw, ctx)
    if res.data.get("action") == "set_permission":
        perm = res.data.get("permission")
        try:
            from src.core.config import load_raw_config, save_config
            cfg = load_raw_config()
            cfg.setdefault("scope", {})["permission"] = perm
            save_config(cfg)
        except Exception as e:
            res.message += f" (persist failed: {e})"
    if res.data.get("action") == "set_plan_mode":
        on = res.data.get("on")
        try:
            from src.core.config import load_raw_config, save_config
            cfg = load_raw_config()
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
    body = await _json_body(request)
    sid = body.get("session_id") or body.get("sessionId") or ""
    todos = body.get("todos")
    if not sid or todos is None:
        raise HTTPException(status_code=400, detail="session_id and todos required")
    from src.core.todo_store import TodoStore as _TS2
    try:
        return _TS2(db_path=_PROJECT_ROOT / "data" / "session.db").write(sid, todos)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/checkpoints")
async def checkpoints_list(session_id: str = ""):
    from src.core.checkpoints import list_checkpoints
    return {"checkpoints": list_checkpoints(session_id)}


@app.post("/api/checkpoints/restore")
async def checkpoints_restore(request: Request):
    body = await _json_body(request)
    from src.core.checkpoints import restore
    return restore(str(body.get("session_id", "")), str(body.get("checkpoint_id", "")))


@app.get("/api/checkpoints/{checkpoint_id}/diff")
async def checkpoints_diff(checkpoint_id: str, session_id: str = ""):
    from src.core.diffutil import checkpoint_diff
    return checkpoint_diff(session_id, checkpoint_id)


@app.post("/api/diff")
async def diff_endpoint(request: Request):
    body = await _json_body(request)
    from src.core.diffutil import unified
    return {"diff": unified(str(body.get("a", "")), str(body.get("b", "")),
                            str(body.get("name_a", "a")), str(body.get("name_b", "b")))}


@app.get("/api/events")
async def events_list(limit: int = 100, kind: str = ""):
    try:
        evs = _event_store().list_events(kind=kind or None, limit=limit)
        return {"events": [e.to_dict() for e in evs]}
    except Exception as e:
        return {"events": [], "error": str(e)}


@app.get("/api/sessions/{session_id}/tree")
async def session_tree(session_id: str):
    try:
        return _session_log().tree(session_id)
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/sessions/{session_id}/fork")
async def session_fork(session_id: str, request: Request):
    body = await _json_body(request, required=False)
    try:
        child = _session_log().fork(session_id)
        if body.get("copy_messages"):
            slog = _session_log()
            for m in slog.get_messages(session_id, limit=10000):
                slog.append(child.id, m)
        return {"ok": True, "id": child.id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/sessions/{session_id}/rewind")
async def session_rewind(session_id: str, request: Request):
    body = await _json_body(request)
    try:
        n = _session_log().rewind(session_id, int(body.get("message_id")))
        return {"ok": True, "deleted": n}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, limit: int = 10000):
    try:
        return {"messages": _session_log().messages_with_ids(session_id, limit=limit)}
    except Exception as e:
        return {"messages": [], "error": str(e)}


@app.get("/api/sessions/{session_id}/mode")
async def session_mode_get(session_id: str):
    return {"mode": _SESSION_MODES.get(session_id, "act")}


@app.post("/api/sessions/{session_id}/mode")
async def session_mode_set(session_id: str, request: Request):
    body = await _json_body(request)
    mode = str(body.get("mode", "act"))
    if mode not in ("act", "plan"):
        raise HTTPException(status_code=400, detail="mode debe ser act|plan")
    _SESSION_MODES[session_id] = mode
    return {"ok": True, "mode": mode}


@app.post("/api/sessions/{session_id}/steer")
async def session_steer(session_id: str, request: Request):
    body = await _json_body(request)
    msg = str(body.get("message", "")).strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message required")
    e = _RUNNING.get(session_id)
    if e is not None:
        e.setdefault("steer", []).append(msg)
        return {"ok": True, "queued": True}
    return {"ok": True, "queued": False}


@app.get("/api/status")
async def status_endpoint():
    running = [{"session_id": sid, "started": e.get("started"),
                "steer_pending": len(e.get("steer", []))} for sid, e in _RUNNING.items()]
    subs = {}
    for sid, sp in _SUB_SPAWNERS.items():
        try:
            kids = sp.list_children()
        except Exception:
            kids = []
        if kids:
            subs[sid] = kids
    modes = dict(_SESSION_MODES)
    return {"running": running, "sub_agents": subs, "modes": modes,
            "counts": {"running": len(running), "sessions_with_subagents": len(subs)}}


@app.get("/api/board/tasks")
async def board_list(status: str = "", assignee: str = ""):
    from src.core.board import get_board
    b = get_board(_PROJECT_ROOT / "data" / "board.db")
    return {"tasks": b.list(status=status, assignee=assignee)}


@app.post("/api/board/tasks")
async def board_create(request: Request):
    body = await _json_body(request)
    from src.core.board import get_board
    b = get_board(_PROJECT_ROOT / "data" / "board.db")
    t = b.create(str(body.get("title", "")).strip(), str(body.get("description", "")),
                 str(body.get("assignee", "")), str(body.get("created_by", "")),
                 int(body.get("priority", 3)), str(body.get("deps", "")))
    return {"ok": True, "task": t}


@app.post("/api/board/{task_id}/claim")
async def board_claim(task_id: str, request: Request):
    body = await _json_body(request, required=False)
    from src.core.board import get_board
    b = get_board(_PROJECT_ROOT / "data" / "board.db")
    return b.claim(task_id, str(body.get("agent", "opencode")))


@app.post("/api/board/{task_id}/update")
async def board_update(task_id: str, request: Request):
    body = await _json_body(request)
    from src.core.board import get_board
    b = get_board(_PROJECT_ROOT / "data" / "board.db")
    return b.update(task_id, str(body.get("status", "")), str(body.get("notes", "")),
                    str(body.get("assignee", "")))


@app.get("/api/worktree")
async def worktree_list(repo: str = ""):
    from src.core.worktree import list_worktrees
    return {"worktrees": list_worktrees(repo or str(_PROJECT_ROOT))}


@app.post("/api/worktree")
async def worktree_add(request: Request):
    body = await _json_body(request)
    from src.core.worktree import add_worktree
    repo = str(body.get("repo", "") or _PROJECT_ROOT)
    path = str(body.get("path", "")).strip()
    branch = str(body.get("branch", "")).strip()
    if not path or not branch:
        raise HTTPException(status_code=400, detail="path y branch requeridos")
    return add_worktree(repo, path, branch, str(body.get("base", "HEAD")))


@app.get("/api/graph/node/{node_id}/cite")
async def graph_cite(node_id: int):
    from src.brain.link import brain_path
    db_path = brain_path() / "knowledge_graph.db"
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="knowledge_graph.db not found")
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT id,title,category,source,created_at,access_count FROM nodes WHERE id=?",
            (node_id,)).fetchone()
    finally:
        con.close()
    if not row:
        raise HTTPException(status_code=404, detail="node not found")
    return {"node_id": int(row["id"]), "title": row["title"], "category": row["category"],
            "source": row["source"], "created_at": row["created_at"],
            "access_count": row["access_count"],
            "citation": f"[n{int(row['id'])}] {row['title']}"}


@app.get("/api/sessions/{session_id}/replay")
async def session_replay(session_id: str, limit: int = 500):
    try:
        evs = _event_store().get_events(session_id=session_id, limit=limit)
        return {"events": [e.to_dict() for e in evs]}
    except Exception as e:
        return {"events": [], "error": str(e)}


@app.get("/api/approvals")
async def approvals_list(session_id: str = ""):
    from src.core.approvals import get_approvals
    return {"approvals": get_approvals().list(session_id), "mode": _auto_apply_mode()}


@app.post("/api/approvals/{approval_id}/approve")
async def approvals_approve(approval_id: str):
    from src.core.approvals import get_approvals
    return get_approvals().resolve(approval_id, True)


@app.post("/api/approvals/{approval_id}/reject")
async def approvals_reject(approval_id: str):
    from src.core.approvals import get_approvals
    return get_approvals().resolve(approval_id, False)


@app.post("/api/approvals/clear")
async def approvals_clear():
    from src.core.approvals import get_approvals
    return {"ok": True, "cleared": get_approvals().clear_resolved()}


def _parse_json_list(text: str) -> list[str]:
    """Extrae una lista de strings de una respuesta del modelo (JSON array o líneas)."""
    import json as _j
    t = (text or "").strip()
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        try:
            arr = _j.loads(t[i:j + 1])
            if isinstance(arr, list):
                out = []
                for x in arr:
                    if isinstance(x, str):
                        out.append(x)
                    elif isinstance(x, dict):
                        out.append(str(x.get("step") or x.get("title") or x))
                    else:
                        out.append(str(x))
                if out:
                    return out[:16]
        except Exception:
            pass
    out = []
    for line in t.splitlines():
        s = line.strip().lstrip("-*•0123456789.) ").strip()
        if s:
            out.append(s)
    return out[:16]


@app.post("/api/plan")
async def plan_endpoint(request: Request):
    body = await _json_body(request)
    task = str(body.get("task", "")).strip()
    if not task:
        raise HTTPException(status_code=400, detail="task required")
    cfg = _load_config_safe()
    router = _get_router(cfg)
    from src.core.provider_base import LLMMessage
    sys_p = ("Sos un planificador técnico. Dada una tarea, devolvé SOLO un array JSON de "
             "strings (3-8 pasos concretos y accionables). Sin texto extra ni markdown.")
    try:
        resp = await asyncio.wait_for(
            router.chat([LLMMessage(role="system", content=sys_p),
                         LLMMessage(role="user", content=task)]), timeout=150)
        steps = _parse_json_list(resp.content or "")
    except Exception as e:
        return {"ok": False, "error": str(e), "steps": []}
    return {"ok": True, "steps": steps, "task": task}


@app.get("/api/skills")
async def skills_list():
    out = []
    base = _PROJECT_ROOT / "data" / "skills"
    if base.is_dir():
        for d in sorted(base.iterdir()):
            md = d / "SKILL.md"
            if md.is_file():
                name, desc = d.name, ""
                try:
                    from src.core.skill_frontmatter import parse_frontmatter_file
                    fm = parse_frontmatter_file(str(md)) or {}
                    name = fm.get("name") or name
                    desc = fm.get("description") or ""
                except Exception:
                    pass
                out.append({"name": name, "description": desc, "path": str(md)})
    return {"skills": out}


@app.get("/api/tools")
async def tools_list():
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "api", cfg)
    return {"tools": [{"name": t.name, "description": t.description, "category": t.category}
                      for t in reg.list()]}


@app.post("/api/worktree/remove")
async def worktree_remove(request: Request):
    body = await _json_body(request)
    from src.core.worktree import remove_worktree
    repo = str(body.get("repo", "") or _PROJECT_ROOT)
    path = str(body.get("path", "")).strip()
    if not path:
        raise HTTPException(status_code=400, detail="path requerido")
    return remove_worktree(repo, path, bool(body.get("force", False)))


@app.get("/api/usage")
async def usage_global():
    from src.core.cost import cost_by_model
    u = _session_log().usage()
    return {**u, "cost": cost_by_model(u.get("by_model", []), _load_config_safe())}


@app.get("/api/sessions/{session_id}/usage")
async def usage_session(session_id: str):
    from src.core.cost import cost_by_model
    u = _session_log().usage(session_id)
    return {**u, "cost": cost_by_model(u.get("by_model", []), _load_config_safe())}


@app.get("/api/plugins")
async def plugins_list():
    from src.core.plugins import list_plugins
    return {"plugins": list_plugins(_PROJECT_ROOT)}


@app.post("/api/plugins/reload")
async def plugins_reload():
    cfg = _load_config_safe()
    scope = _build_scope(cfg)
    reg = _build_registry(scope, "plugins", cfg)
    loaded = [t.name for t in reg.list() if t.category == "plugin"]
    return {"ok": True, "loaded": loaded}


@app.get("/api/permission")
async def permission_get():
    from src.core.config import load_config
    cfg = load_config()
    perm = cfg.get("scope", {}).get("permission", "workspace_write")
    return {"permission": perm, "options": ["read_only", "workspace_write", "full"]}

@app.put("/api/permission")
async def permission_put(request: Request):
    body = await _json_body(request)
    perm = (body.get("permission") or "").lower()
    if perm not in ("read_only", "workspace_write", "full"):
        raise HTTPException(status_code=400, detail="permission must be read_only|workspace_write|full")
    from src.core.config import load_raw_config, save_config
    cfg = load_raw_config()
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
    import mimetypes as _mt
    from uuid import uuid4
    limit = _UPLOADS_MAX_BYTES
    # Chequeo temprano de tamaño si el framework lo expone (sin bufferizar todo).
    try:
        if getattr(file, "size", None) and int(file.size) > limit:
            raise HTTPException(status_code=413, detail=f"archivo muy grande (max {limit} bytes)")
    except HTTPException:
        raise
    except Exception:
        pass
    # Leer por chunks y abortar temprano al pasar el limite (evita presion de memoria).
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail=f"archivo muy grande (max {limit} bytes)")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="archivo vacio")
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
    try:
        _cl = int(request.headers.get("content-length", "0") or 0)
    except Exception:
        _cl = 0
    if _cl > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio demasiado grande (max 25MB)")
    audio = await request.body()
    if len(audio) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="audio demasiado grande (max 25MB)")
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
    body = await _json_body(request)
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
    # Sanitizar: sin CR/LF (evita inyeccion de lineas/vars en .env) + cap de longitud.
    value = (value or "").replace("\r", "").replace("\n", "").strip()[:8192]
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
    body = await _json_body(request)
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


_CLOSE_TASKS: set = set()
_RETIRED_ROUTERS: list = []


def _close_router(router) -> None:
    """Cierra los AsyncClient httpx de los providers (evita fuga de FDs)."""
    try:
        providers = router.providers()
    except Exception:
        return
    for p in providers.values():
        close = getattr(p, "aclose", None) or getattr(p, "close", None)
        if close is None:
            continue
        try:
            res = close()
            if asyncio.iscoroutine(res):
                try:
                    loop = asyncio.get_running_loop()
                    _t = loop.create_task(res)
                    _CLOSE_TASKS.add(_t)
                    _t.add_done_callback(_CLOSE_TASKS.discard)
                except RuntimeError:
                    # Sin loop en ejecucion: cerrar sincronicamente (evita coroutine sin await).
                    try:
                        asyncio.run(res)
                    except Exception:
                        pass
        except Exception:
            pass


def _schedule_router_close(router, delay_s: float = 30.0) -> None:
    """Cierra un router DIFERIDO: no cerrarlo mientras requests en vuelo lo usan."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _close_router(router)
        return

    async def _later():
        await asyncio.sleep(delay_s)
        _close_router(router)

    try:
        _t = loop.create_task(_later())
        _CLOSE_TASKS.add(_t)
        _t.add_done_callback(_CLOSE_TASKS.discard)
    except Exception:
        pass


def _get_router(cfg: dict):
    """Router cacheado por firma de config (no reconstruir por request)."""
    import hashlib as _hl
    import json as _j
    try:
        sig = _hl.md5(_j.dumps(
            {"p": (cfg or {}).get("providers", {}), "r": (cfg or {}).get("router", {})},
            sort_keys=True, default=str,
        ).encode()).hexdigest()
    except Exception:
        sig = ""
    cached = _state.get("router")
    if cached is not None and _state.get("router_sig") == sig:
        return cached
    if cached is not None:
        # RETIRAR sin cerrar: cerrar aqui mataba requests en vuelo (flap de firma).
        # Los routers retirados se cierran al shutdown.
        _RETIRED_ROUTERS.append(cached)
    router = _build_router(cfg)
    _state["router"] = router
    _state["router_sig"] = sig
    return router


def _invalidate_router() -> None:
    r = _state.pop("router", None)
    _state.pop("router_sig", None)
    if r is not None:
        _RETIRED_ROUTERS.append(r)


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


def _build_scope(cfg: dict, agent_id: str | None = None, permission_override: str | None = None):
    """Scope jail desde config.scope (allowed_roots + deny_patterns + permission).

    Fase DECK: antes no se pasaba permission= y ScopeConfig quedaba en el default
    workspace_write -> 'full' no liberaba nada. Con permission=full se anaden
    todas las unidades y la red.

    Aislamiento por ayudante: si agent_id, su workspace propio reemplaza ./workspace
    como raiz escribible (./data sigue compartido para sesiones/spill/cerebro).
    """
    from src.core.scope import Scope, ScopeConfig
    scope_cfg = cfg.get("scope", {})
    permission = str(scope_cfg.get("permission", "workspace_write"))
    if permission_override:
        permission = permission_override
    roots = list(scope_cfg.get("allowed_roots", ["./workspace", "./data"]))
    if agent_id:
        try:
            ws = _agent_registry().resolve(agent_id).get("workspace_abs")
            if ws and ws not in roots:
                roots.insert(0, ws)
        except Exception:
            pass
    roots = tuple(roots)
    allow_net = bool(scope_cfg.get("allow_network", False))
    deny = tuple(scope_cfg.get("deny_patterns", ["**/.env", "**/*.key"]))
    if permission == "full":
        roots = roots + tuple(r for r in _full_access_roots() if r not in roots)
        allow_net = True
        # Acceso total: el usuario lo pidió -> sin deny por defecto (configurable).
        deny = tuple(scope_cfg.get("deny_patterns_full", []))
    return Scope(ScopeConfig(
        allowed_roots=roots,
        deny_patterns=deny,
        max_read_bytes=int(scope_cfg.get("max_read_bytes", 1_048_576)),
        allow_network=allow_net,
        allow_mutating_without_confirm=bool(scope_cfg.get("allow_mutating_without_confirm", False)) or permission == "full",
        require_confirm_mutating=bool(scope_cfg.get("require_confirm_mutating", False)) and permission != "full",
        permission=permission,
    ), project_root=_PROJECT_ROOT)


async def _helper_run(task: str, *, gema: str = "", agent_id: str | None = None, cfg: dict, router, images: list | None = None) -> str:
    """Ejecuta un ayudante/gema como sub-loop aislado y devuelve su salida (JSON).

    El LLM se asigna automáticamente (model_assign) según lo disponible en la PC,
    salvo que el ayudante tenga un modelo explícito. El sub-loop NO recibe las tools
    de delegación (evita recursión) y corre en el scope del ayudante (workspace propio).
    """
    import json as _j
    import uuid as _uuid
    from src.core.agent_loop import AgentLoop
    from src.core.model_assign import assign_model
    try:
        from src.core.gema_profiles import system_prompt_for_gema, hint_for_gema
    except Exception:
        def system_prompt_for_gema(_g):  # type: ignore
            return ""
        def hint_for_gema(_g):  # type: ignore
            return None
    agent = _agent_registry().resolve(agent_id) if agent_id else None
    use_gema = (gema or (agent or {}).get("gema") or "").strip()
    sys_prompt = (agent or {}).get("system_prompt") or ""
    if use_gema and not sys_prompt:
        sys_prompt = system_prompt_for_gema(use_gema)
    assign = assign_model(router, gema=use_gema, explicit=(agent or {}).get("model") or None, overrides=_assign_overrides(cfg))
    model = assign.get("model") or None
    hint = hint_for_gema(use_gema) if use_gema else None
    sid = f"helper-{use_gema or (agent or {}).get('id') or 'x'}-{_uuid.uuid4().hex[:8]}"
    slog = _session_log()
    try:
        slog.create_session(project=f"helper:{use_gema or 'agent'}", session_id=sid)
    except Exception:
        pass
    scope = _build_scope(cfg, agent_id)
    sub_reg = _build_registry(scope, sid, cfg, router=router, helpers=False)
    loop = AgentLoop(
        provider_router=router, tool_registry=sub_reg, scope=scope, session_log=slog,
        event_store=_event_store(), compaction=_build_compaction(cfg), hooks=_build_hooks(cfg),
        distill_hook=_build_distill_hook(cfg),
        max_iterations=int((cfg.get("agent_loop", {}) or {}).get("max_iterations", 25)),
    )
    res = await loop.run(task=task, session_id=sid, hint=hint, model=model, system_prompt=sys_prompt or None, images=images or None)
    return _j.dumps({
        "gema": use_gema, "agent": (agent or {}).get("id"),
        "model": model, "provider": assign.get("provider"), "assign_reason": assign.get("reason"),
        "success": res.success, "output": res.final_output,
    }, ensure_ascii=False)


_SUB_SPAWNERS: dict = {}

# Runs activos por sesión (para steer/queue y status real).
_RUNNING: dict = {}
_SESSION_MODES: dict = {}  # session_id -> "act" | "plan"


def _steer_drain(session_id: str) -> list:
    e = _RUNNING.get(session_id)
    if not e:
        return []
    out = e.get("steer", [])[:]
    e["steer"] = []
    return out


def _auto_apply_mode(cfg: dict | None = None) -> str:
    """auto | assisted (default) | approve. `approve` encola mutaciones para aprobación."""
    try:
        cfg = cfg if isinstance(cfg, dict) else _load_config_safe()
        return str((cfg.get("agent_loop") or {}).get("auto_apply", "assisted"))
    except Exception:
        return "assisted"


def _get_spawner(session_id: str, router, scope):
    """Spawner de sub-agentes por sesión (reusa el router para el LLM del hijo)."""
    sp = _SUB_SPAWNERS.get(session_id)
    if sp is None:
        from src.core.provider_base import LLMMessage
        from src.core.sub_agent_spawner import SubAgentSpawner

        async def _exec(task: str, gem: str = "auto", context: str = ""):
            sys_p = ("Eres un sub-agente de nexus-v3. Resolvés la tarea delegada y devolvés "
                     "un resultado conciso y directo, sin rodeos.")
            user = (context + "\n\n" if context else "") + task
            r = await router.chat([LLMMessage(role="system", content=sys_p),
                                   LLMMessage(role="user", content=user)])
            return r.content or ""

        roots = tuple(getattr(getattr(scope, "cfg", None), "allowed_roots", ()) or ())
        sp = SubAgentSpawner(executor=_exec, scope_roots=roots, parent_session_id=session_id)
        _SUB_SPAWNERS[session_id] = sp
    return sp


def _build_registry(scope, session_id: str, cfg: dict | None = None, router=None, helpers: bool = True):
    """Toolset estandar del harness, jail-ado por scope (closures que usan scope/session_id)."""
    from src.core.tool_registry import ToolRegistry
    registry = ToolRegistry()

    def _tool_call_log(rec: dict) -> None:
        try:
            _event_store().append_event(kind="tool_call", data=rec, session_id=session_id)
        except Exception:
            pass

    registry.call_log = _tool_call_log
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
        scope.check_tool("write_file", {"path": path, "content": content})

        def _do() -> str:
            try:
                from src.core.checkpoints import snapshot
                if p.is_file():
                    snapshot(session_id, p)
            except Exception:
                pass
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"escrito {path} ({len(content.encode('utf-8'))} bytes)"

        if _auto_apply_mode(cfg) == "approve":
            from src.core.approvals import get_approvals
            pr = get_approvals().propose(session_id, "write_file", {"path": path, "content": content},
                                         preview=content, apply=_do)
            return (f"[pendiente de aprobacion id={pr['id']}] write_file {path} ({len(content)} chars) — "
                    f"el usuario debe aprobar en la UI (Aprobaciones).")
        return _do()

    def edit_file_tool(path: str, old: str, new: str, replace_all: bool = False) -> str:
        """Reemplazo quirurgico (str_replace) en un archivo. `old` debe existir;
        si aparece >1 vez y replace_all=False -> error (evita ediciones ambiguas)."""
        p = scope.check_path(path)
        scope.check_tool("edit", {"path": path, "content": new})
        if not p.is_file():
            raise FileNotFoundError(f"no existe o no es archivo: {path}")
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            raise ValueError(f"texto a reemplazar no encontrado en {path}")
        if n > 1 and not replace_all:
            raise ValueError(f"el texto aparece {n} veces en {path}; agregá más contexto o usá replace_all=true")

        def _do() -> str:
            try:
                from src.core.checkpoints import snapshot
                snapshot(session_id, p)
            except Exception:
                pass
            new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            p.write_text(new_text, encoding="utf-8")
            return f"editado {path} ({n if replace_all else 1} reemplazo/s)"

        if _auto_apply_mode(cfg) == "approve":
            from src.core.approvals import get_approvals
            from src.core.diffutil import unified
            _new = text.replace(old, new) if replace_all else text.replace(old, new, 1)
            pr = get_approvals().propose(session_id, "edit", {"path": path, "old": old, "new": new},
                                         preview=unified(text, _new, name_a=path, name_b=path + " (propuesto)"),
                                         apply=_do)
            return (f"[pendiente de aprobacion id={pr['id']}] edit {path} — "
                    f"el usuario debe aprobar en la UI (Aprobaciones).")
        return _do()

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
            entries.append({"name": child.name, "type": "dir" if child.is_dir() else "file", "path": _rel(child)})
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
        scope.check_tool("exec", {"command": cmd})
        cwd_p = scope.check_path(cwd or "./workspace")
        if not cwd_p.exists():
            cwd_p.mkdir(parents=True, exist_ok=True)
        if not cwd_p.is_dir():
            raise NotADirectoryError("cwd debe ser directorio dentro de allowed_roots")
        _jail = _exec_jail(cmd, cwd_p, scope)
        if _jail is not None:
            raise PermissionError(_jail)
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
    registry.register_function(edit_file_tool, name="edit", description="Reemplazo quirúrgico en un archivo (str_replace): reemplaza `old` por `new`; `old` debe ser único (o usá replace_all=true).", category="local")

    async def code_search_tool(query: str, path: str = ".", include: str = "", limit: int = 20) -> str:
        """Busca código (ripgrep/grep) en un directorio; devuelve coincidencias archivo:línea."""
        import json as _j
        p = scope.check_path(path or ".")
        from src.tools.code_search import code_search as _cs
        res = await _cs(query, path=str(p), include=include, limit=max(1, min(50, int(limit or 20))))
        return _j.dumps(res, ensure_ascii=False)
    registry.register_function(code_search_tool, name="code_search",
                               description="Busca código (regex/texto) en el proyecto; devuelve coincidencias con archivo:línea.",
                               category="local")
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
        # NO tragar ImportError (fail closed) + validar CADA hop de redirect (evita SSRF via 302).
        from src.tools.web_fetch import is_safe_url
        import urllib.request, urllib.error, urllib.parse, re
        timeout = max(2, min(30, int(timeout)))
        _opener = urllib.request.build_opener()
        _opener.add_handler(type("_NoRedir", (urllib.request.HTTPRedirectHandler,), {
            "redirect_request": lambda self, *a, **k: None}))
        current = url
        try:
            for _hop in range(6):
                if not is_safe_url(current):
                    raise PermissionError(f"URL bloqueada (SSRF/metadata/privada): {current}")
                req = urllib.request.Request(current, headers={"User-Agent": "Nexus-v3/1.0 web_fetch"})
                try:
                    with _opener.open(req, timeout=timeout) as r:
                        data = r.read(15000)
                        ctype = r.headers.get("content-type", "")
                        text = data.decode("utf-8", errors="replace")
                        if "text/html" in ctype:
                            text = re.sub(r"<[^>]+>", " ", text)
                            text = re.sub(r"\s+", " ", text)
                        return text[:15000]
                except urllib.error.HTTPError as he:
                    if he.code in (301, 302, 303, 307, 308) and he.headers.get("Location"):
                        current = urllib.parse.urljoin(current, he.headers["Location"])
                        continue
                    raise
            raise RuntimeError("web_fetch: demasiados redirects")
        except (PermissionError, RuntimeError):
            raise
        except Exception as e:
            raise RuntimeError(f"web_fetch failed: {e}")
    registry.register_function(web_fetch, name="web_fetch", description="Fetch URL and return text (network gate, truncates 15k)", category="local")

    async def web_search_tool(query: str, limit: int = 6) -> str:
        """Busca en la web (open source: SearXNG via SEARXNG_URL, fallback Wikipedia)."""
        import json as _j
        scope.check_tool("web_fetch", {"url": "search"})  # network gate
        from src.tools.web_search import web_search as _ws
        res = await _ws(query, limit=max(1, min(10, int(limit or 6))))
        return _j.dumps(res, ensure_ascii=False)
    registry.register_function(web_search_tool, name="web_search",
                               description="Busca en la web y devuelve título+url+snippet (SearXNG open source; fallback Wikipedia).",
                               category="local")

    # --- learn (Método OPCODE: vía única de escritura de conocimiento) ---
    async def learn_tool(content: str, topic_key: str = "", category: str = "general",
                         tags: str = "") -> str:
        """Persist knowledge in the knowledge graph (OPCODE method).

        Use AFTER learning something durable: findings, decisions, preferences,
        lessons. content = the knowledge (1-3 sentences or short block);
        topic_key = stable slug for evolving topics (e.g. 'auth/jwt-rotation')
        enables UPSERT (revision++); tags = comma-separated."""
        import json as _j
        from src.core.knowledge_memory import get_knowledge_memory
        mem = ((cfg or {}).get("memory", {}) or {})
        if not mem.get("agent_write_enabled", True):
            return _j.dumps({"ok": False, "skipped": "memory.agent_write_enabled=false"}, ensure_ascii=False)
        _c = (content or "").strip()
        _min = int(mem.get("learn_min_chars", 1))
        if len(_c) < _min:
            raise ValueError(f"content demasiado corto para learn (min {_min} caracteres)")
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        res = await asyncio.to_thread(
            lambda: get_knowledge_memory().learn(
                content.strip(), topic_key=(topic_key or "").strip(),
                category=(category or "general").strip(), tags=tag_list,
                title=content.strip().split("\n")[0][:80], source="agent",
            )
        )
        return _j.dumps(res, ensure_ascii=False)
    registry.register_function(
        learn_tool, name="learn",
        description="Persist knowledge in the knowledge graph (OPCODE: vía única de escritura). Use after learning something durable: findings, decisions, lessons.",
        category="local",
        parameters={"type": "object", "properties": {
            "content": {"type": "string", "description": "The knowledge to persist (short block)."},
            "topic_key": {"type": "string", "description": "Stable slug for evolving topics (UPSERT). Empty = append with dedupe."},
            "category": {"type": "string", "description": "general|concept|lesson|architecture|fix|fact"},
            "tags": {"type": "string", "description": "Comma-separated tags."},
        }, "required": ["content"], "additionalProperties": False},
    )

    async def recall_tool(query: str, limit: int = 5) -> str:
        """Search prior knowledge in the local graph before answering from history."""
        import json as _j
        from src.core.knowledge_memory import get_knowledge_memory
        hits = await asyncio.to_thread(
            lambda: get_knowledge_memory().fts_search(
                query, limit=max(1, min(10, int(limit)))
            )
        )
        return _j.dumps({"query": query, "results": hits}, ensure_ascii=False)
    registry.register_function(
        recall_tool, name="recall",
        description="Search the local knowledge graph with FTS5 before answering questions about prior decisions, preferences, lessons, or project history.",
        category="local",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords for prior knowledge."},
            "limit": {"type": "integer", "description": "Maximum results (1-10)."},
        }, "required": ["query"], "additionalProperties": False},
    )

    def mcp_list(server: str = "") -> str:
        """Lista los servidores MCP del bundle y sus tools (server vacío = solo servidores)."""
        import json as _j
        from src.core.mcp_client import get_mcp_client
        cli = get_mcp_client(_PROJECT_ROOT)
        if not server:
            return _j.dumps({"servers": cli.list_servers()}, ensure_ascii=False)
        return _j.dumps({"server": server, "tools": cli.list_tools(server)}, ensure_ascii=False)
    registry.register_function(
        mcp_list, name="mcp_list",
        description="Lista servidores MCP del bundle portable y sus tools. server='' lista servidores; server='<nombre>' lista sus tools (arranca el servidor just-in-time).",
        category="mcp",
        parameters={"type": "object", "properties": {
            "server": {"type": "string", "description": "Nombre del servidor MCP (vacío = listar servidores)."},
        }, "required": [], "additionalProperties": False},
    )

    async def mcp_call(server: str, tool: str, arguments: dict | None = None) -> str:
        """Llama una tool de un servidor MCP del bundle (stdio, just-in-time)."""
        import json as _j
        from src.core.mcp_client import get_mcp_client
        cli = get_mcp_client(_PROJECT_ROOT)
        res = await asyncio.to_thread(cli.call, server, tool, arguments or {})
        return _j.dumps(res, ensure_ascii=False)
    registry.register_function(
        mcp_call, name="mcp_call",
        description="Llama una tool de un servidor MCP del bundle. Usa mcp_list para descubrir server/tool/args.",
        category="mcp",
        parameters={"type": "object", "properties": {
            "server": {"type": "string", "description": "Nombre del servidor MCP."},
            "tool": {"type": "string", "description": "Nombre de la tool."},
            "arguments": {"type": "object", "description": "Argumentos de la tool (JSON)."},
        }, "required": ["server", "tool"], "additionalProperties": False},
    )

    if helpers and router is not None:
        def gema_list() -> str:
            """Lista las gemas especializadas y el LLM que se les asignaría automáticamente."""
            import json as _j
            from src.core.gema_profiles import list_gemas
            from src.core.model_assign import assign_model
            out = []
            for g in list_gemas():
                a = assign_model(router, gema=g["name"], model_hint=g.get("model_hint", ""), overrides=_assign_overrides(cfg))
                out.append({"name": g["name"], "description": g["description"], "model_hint": g.get("model_hint"),
                            "assigned_model": a.get("model"), "provider": a.get("provider")})
            return _j.dumps({"gemas": out}, ensure_ascii=False)
        registry.register_function(
            gema_list, name="gema_list",
            description="Lista las gemas especializadas disponibles y el LLM que se les asignaría según lo disponible en la PC.",
            category="local",
            parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )

        def agent_list() -> str:
            """Lista los ayudantes configurados y el LLM que usarían (explícito o auto-asignado)."""
            import json as _j
            from src.core.model_assign import assign_model
            out = []
            for a in _agent_registry().list():
                asg = assign_model(router, gema=a.get("gema", ""), explicit=a.get("model") or None, overrides=_assign_overrides(cfg))
                out.append({"id": a.get("id"), "name": a.get("name"), "gema": a.get("gema", ""),
                            "model": a.get("model") or "", "assigned_model": asg.get("model"), "provider": asg.get("provider")})
            return _j.dumps({"agents": out}, ensure_ascii=False)
        registry.register_function(
            agent_list, name="agent_list",
            description="Lista los ayudantes configurados y el LLM que usarían. Delega con agent_run.",
            category="local",
            parameters={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        )

        async def gema_run(gema: str, task: str) -> str:
            """Delega una tarea a una gema especializada (sub-loop aislado, LLM auto-asignado)."""
            return await _helper_run(task, gema=gema, cfg=(cfg or {}), router=router, images=getattr(registry, "current_images", None))
        registry.register_function(
            gema_run, name="gema_run",
            description="Delega una tarea a una gema especializada. El LLM se asigna automáticamente según lo disponible. Usa gema_list para ver las gemas.",
            category="local",
            parameters={"type": "object", "properties": {"gema": {"type": "string"}, "task": {"type": "string"}},
                        "required": ["gema", "task"], "additionalProperties": False},
        )

        async def agent_run(agent_id: str, task: str) -> str:
            """Delega una tarea a un ayudante configurado (su gema/prompt/LLM y workspace aislado)."""
            return await _helper_run(task, agent_id=agent_id, cfg=(cfg or {}), router=router, images=getattr(registry, "current_images", None))
        registry.register_function(
            agent_run, name="agent_run",
            description="Delega una tarea a un ayudante por id; usa su system prompt, su LLM y su workspace aislado. Usa agent_list para verlos.",
            category="local",
            parameters={"type": "object", "properties": {"agent_id": {"type": "string"}, "task": {"type": "string"}},
                        "required": ["agent_id", "task"], "additionalProperties": False},
        )
    async def sub_agent_tool(task: str, mode: str = "one-shot", gema: str = "auto", context: str = "") -> str:
        """Delega una tarea a un sub-agente aislado (contexto propio, depth limit 3).
        mode='continuable' lo mantiene vivo para más mensajes vía sub_agent_message."""
        if router is None:
            raise RuntimeError("sub_agent requiere router (helpers=True)")
        import json as _json
        from src.core.sub_agent_spawner import SubAgentSpec
        sp = _get_spawner(session_id, router, scope)
        spec = SubAgentSpec(task=task, gem=gema, context=context,
                            mode=mode if mode in ("one-shot", "continuable") else "one-shot")
        res = await sp.spawn_child(spec)
        return _json.dumps({"child_id": res.child_id, "status": res.status.value,
                            "result": res.result, "error": res.error}, ensure_ascii=False, default=str)

    def sub_agent_message_tool(child_id: str, message: str) -> str:
        """Envía un mensaje a un sub-agente continuable (child_id de sub_agent)."""
        sp = _SUB_SPAWNERS.get(session_id)
        if sp is None:
            raise RuntimeError("no hay sub-agentes en esta sesión")
        return "ok" if sp.send_message(child_id, message) else "child_id no encontrado"

    def sub_agent_list_tool() -> str:
        """Lista los sub-agentes de la sesión (child_id, estado, profundidad)."""
        import json as _json
        sp = _SUB_SPAWNERS.get(session_id)
        return _json.dumps(sp.list_children() if sp else [], ensure_ascii=False, default=str)

    registry.register_function(
        sub_agent_tool, name="sub_agent",
        description="Delega una tarea a un sub-agente aislado (contexto propio, depth limit 3). mode='continuable' lo mantiene vivo para más mensajes con sub_agent_message.",
        category="local",
        parameters={"type": "object", "properties": {
            "task": {"type": "string"},
            "mode": {"type": "string", "enum": ["one-shot", "continuable"]},
            "gema": {"type": "string"}, "context": {"type": "string"}},
            "required": ["task"], "additionalProperties": False},
    )
    registry.register_function(
        sub_agent_message_tool, name="sub_agent_message",
        description="Envía un mensaje a un sub-agente continuable (child_id devuelto por sub_agent).",
        category="local",
        parameters={"type": "object", "properties": {"child_id": {"type": "string"}, "message": {"type": "string"}},
                    "required": ["child_id", "message"], "additionalProperties": False},
    )
    registry.register_function(
        sub_agent_list_tool, name="sub_agent_list",
        description="Lista los sub-agentes de la sesión (child_id, estado, profundidad).",
        category="local",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    # Plugins de terceros (./plugins/*.py con register(registry)). Best-effort.
    try:
        from src.core.plugins import load_plugins
        load_plugins(registry, _PROJECT_ROOT)
    except Exception:
        pass
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
                          model: str | None = None, force_tools: bool = False,
                          agent_id: str | None = None, on_delta=None):
    """Un run de AgentLoop con la construccion estandar (config segura)."""
    from src.core.config import load_config
    from src.core.agent_loop import AgentLoop
    cfg = _load_config_safe()
    router = _get_router(cfg)
    # Plan mode: el turno corre con alcance read_only (sin mutaciones).
    _plan = _SESSION_MODES.get(session_id) == "plan"
    scope = _build_scope(cfg, agent_id, permission_override="read_only" if _plan else None)
    registry = _build_registry(scope, session_id, cfg, router=router)
    agent = _agent_registry().resolve(agent_id) if agent_id else None
    _gema = (agent or {}).get("gema") or ""
    _sys = (agent or {}).get("system_prompt") or ""
    if _gema:
        try:
            from src.core.gema_profiles import system_prompt_for_gema, hint_for_gema
            if not _sys:
                _sys = system_prompt_for_gema(_gema)
            if not hint:
                hint = hint_for_gema(_gema) or hint
        except Exception:
            pass
    loop = AgentLoop(provider_router=router, tool_registry=registry, scope=scope, session_log=_session_log(),
                     event_store=_event_store(), compaction=_build_compaction(cfg), hooks=_build_hooks(cfg), distill_hook=_build_distill_hook(cfg))
    result = await loop.run(task=message, session_id=session_id, hint=hint,
                            force_tools=force_tools,
                            model=model or (agent or {}).get("model") or None,
                            system_prompt=_sys or None, on_delta=on_delta,
                            steer=(lambda: _steer_drain(session_id)) if session_id else None)
    await _learning_record(message, session_id, result, hint=hint, model=model)
    return result


@app.post("/api/chat")

async def chat(request: Request):
    """
    Body: { message: str, session_id?: str, hint?: str, model?: str }
    Runs one AgentLoop iteration. If providers unavailable, echoes with diagnostics.
    """
    body = await _json_body(request)
    message = body.get("message") or body.get("content") or ""
    attachments = body.get("attachments") or []
    if not message and not attachments:
        raise HTTPException(status_code=400, detail="message required")
    session_id = body.get("session_id")
    hint = body.get("hint")
    agent_id = body.get("agent_id")
    _agent_cfg = _agent_registry().resolve(agent_id) if agent_id else None
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
    _project = agent_id or "default"
    if not session_id:
        sess = slog.create_session(project=_project)
        session_id = sess.id
    else:
        # Ensure exists: get_messages devuelve [] para sesión inexistente (NO lanza),
        # así que se verifica con get_session para evitar IntegrityError (FK) al append.
        try:
            if slog.get_session(session_id) is None:
                # Crear con el id del cliente => estable entre turnos (memoria multi-turno).
                sess = slog.create_session(project=_project, session_id=session_id)
        except Exception:
            sess = slog.create_session(project=_project)
            session_id = sess.id

    # El mensaje del usuario y la respuesta del asistente los persiste el AgentLoop
    # (session_log=slog), para que el historial se cargue entre turnos (memoria multi-turno).
    # Try to run AgentLoop if providers are configured
    try:
        from src.core.config import load_config
        from src.core.agent_loop import AgentLoop

        cfg = load_config()
        router = _get_router(cfg)

        # Quick health probe with short timeout — don't block chat if providers offline
        health = {}
        if hasattr(router, "health_all"):
            try:
                health = await asyncio.wait_for(router.health_all(), timeout=3.0)
            except Exception:
                health = {}
        any_healthy = any(
            (v.get("ok") if isinstance(v, dict) else bool(v))
            for v in (health.values() if isinstance(health, dict) else [])
        )
        # If no provider resolves at all (no models registered), go fallback immediately
        # Otherwise attempt AgentLoop even if health check failed — loop will error and fallback will handle
        if not router.resolve(task_hint=hint or message) and not any_healthy:
            raise RuntimeError("no healthy provider — echo fallback")

        # Scope + tools (mismo helper que /v1, /ws y /stream)
        scope = _build_scope(cfg, agent_id)
        registry = _build_registry(scope, session_id, cfg, router=router)

        _gema = (_agent_cfg or {}).get("gema") or ""
        _sys = (_agent_cfg or {}).get("system_prompt") or ""
        if _gema:
            try:
                from src.core.gema_profiles import system_prompt_for_gema, hint_for_gema
                if not _sys:
                    _sys = system_prompt_for_gema(_gema)
                if not hint:
                    hint = hint_for_gema(_gema) or hint
            except Exception:
                pass

        loop = AgentLoop(provider_router=router, tool_registry=registry, scope=scope, session_log=slog,
                         event_store=_event_store(), compaction=_build_compaction(cfg), hooks=_build_hooks(cfg), distill_hook=_build_distill_hook(cfg))
        result = await loop.run(task=message, session_id=session_id, hint=hint,
                                force_tools=bool(body.get("force_tools", False)),
                                model=body.get("model") or (_agent_cfg or {}).get("model") or None,
                                images=images or None,
                                system_prompt=_sys or None)

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
            "agent": (_agent_cfg or {}).get("id"),
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


@app.get("/api/gemas")
async def list_gemas_endpoint():
    from src.core.gema_profiles import list_gemas
    return {"gemas": list_gemas()}


@app.get("/api/models/assign")
async def assign_model_endpoint(gema: str = "", agent: str = "", model: str = ""):
    from src.core.model_assign import assign_model
    cfg = _load_config_safe()
    router = _get_router(cfg)
    explicit = model or None
    if agent and not explicit:
        a = _agent_registry().get(agent)
        explicit = (a or {}).get("model") or None
        gema = gema or (a or {}).get("gema") or ""
    mh = ""
    if gema:
        try:
            from src.core.gema_profiles import GEMA_PROFILES
            p = GEMA_PROFILES.get(gema)
            mh = p.model_hint if p else ""
        except Exception:
            mh = ""
    return {"assign": assign_model(router, gema=gema, model_hint=mh, explicit=explicit, overrides=_assign_overrides(cfg))}


@app.get("/api/agents")
async def list_agents():
    return {"agents": _agent_registry().list()}


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    a = _agent_registry().get(agent_id)
    if a is None:
        raise HTTPException(status_code=404, detail="ayudante no encontrado")
    return {"agent": a}


@app.post("/api/agents")
async def create_agent(request: Request):
    body = await _json_body(request)
    try:
        agent = _agent_registry().create(
            name=body.get("name") or "",
            model=body.get("model", ""), provider=body.get("provider", ""),
            system_prompt=body.get("system_prompt", ""), description=body.get("description", ""),
            emoji=body.get("emoji", ""), color=body.get("color", ""), gema=body.get("gema", ""),
            toolsets=body.get("toolsets", []), skills=body.get("skills", []), mcp=body.get("mcp", []),
        )
        return {"ok": True, "agent": agent}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/agents/{agent_id}")
async def update_agent(agent_id: str, request: Request):
    body = await _json_body(request)
    try:
        return {"ok": True, "agent": _agent_registry().update(agent_id, body or {})}
    except KeyError:
        raise HTTPException(status_code=404, detail="ayudante no encontrado")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str):
    try:
        _agent_registry().delete(agent_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="ayudante no encontrado")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/sessions/{session_id}/distill")
async def distill_session_endpoint(session_id: str):
    """Destila una sesión al grafo a demanda (determinista + enriquecimiento LLM, idempotente)."""
    if _session_log().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    res = await _distill_run(_load_config_safe(), session_id, wait_llm=True, llm_timeout=75)
    return {"ok": True, "result": res}


@app.post("/api/sessions/{session_id}/end")
async def end_session_endpoint(session_id: str):
    """Cierra una sesión: destila su conocimiento al grafo (red de seguridad, background)."""
    if _session_log().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    res = await _distill_run(_load_config_safe(), session_id, wait_llm=False)
    return {"ok": True, "result": res}


@app.post("/api/sessions/{session_id}/fork")
async def fork_session_endpoint(session_id: str):
    """Crea una rama (hijo) de la sesion — sesiones en arbol (pi-style)."""
    if _session_log().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    child = _session_log().fork(session_id)
    return {"ok": True, "session": child.to_dict() if hasattr(child, "to_dict") else {"id": child.id}}


@app.post("/api/sessions/{session_id}/branch")
async def branch_session_endpoint(session_id: str, copy_messages: bool = False):
    """Rama con (opcional) copia de mensajes — sesiones en arbol (pi-style)."""
    if _session_log().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    child = _session_log().branch(session_id, copy_messages=copy_messages)
    return {"ok": True, "session": child.to_dict() if hasattr(child, "to_dict") else {"id": child.id}}


@app.get("/api/sessions/{session_id}/tree")
async def session_tree_endpoint(session_id: str):
    """Arbol de la sesion: ancestros + hijos (pi-style)."""
    if _session_log().get_session(session_id) is None:
        raise HTTPException(status_code=404, detail="sesión no encontrada")
    return _session_log().tree(session_id)


@app.get("/api/runtime")
async def runtime_endpoint():
    """Estado de eficiencia: concurrencia (free-threaded, workers) + tuning de providers."""
    out: dict = {}
    try:
        from src.core.concurrency import info as _ci
        out["concurrency"] = _ci()
    except Exception as e:
        out["concurrency"] = {"error": str(e)[:120]}
    try:
        cfg = _state.get("config") or _load_config_safe()
        provs = (cfg.get("providers", {}) or {})
        out["tuning"] = {
            "ollama": {"keep_alive": (provs.get("ollama", {}) or {}).get("keep_alive", "30m"),
                       "options": (provs.get("ollama", {}) or {}).get("options", {})},
            "llamacpp": {"max_tokens": (provs.get("llamacpp", {}) or {}).get("max_tokens", 2048),
                         "cache_prompt": True},
            "cloud": {"strict_tools": (provs.get("cloud", {}) or {}).get("strict_tools", False)},
            "sticky_model": __import__("os").environ.get("NEXUS_STICKY_MODEL", "1"),
            "parallel_tools": __import__("os").environ.get("NEXUS_PARALLEL_TOOLS", "1"),
        }
    except Exception as e:
        out["tuning"] = {"error": str(e)[:120]}
    return out


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
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                user_text = " ".join(str(p.get("text","")) for p in c if isinstance(p, dict))
            else:
                user_text = str(c or "")
            break
    if not user_text and messages and isinstance(messages[-1], dict):
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
        reply = "Error: no se pudo completar la solicitud (ver logs del servidor)."
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
        router = _get_router(cfg)
        models = [r.id for r in (router.list_models() or []) if getattr(r, "id", None)]
        if not models:
            models = ["auto", "nexus-v3"]
    except Exception:
        models = ["auto","nexus-v3"]
    return JSONResponse({"object":"list","data":[{"id":m,"object":"model","created":0,"owned_by":"nexus"} for m in models]})
@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    # Guardia anti cross-site WebSocket hijacking: CORS/middleware http NO cubren WS.
    origin = ws.headers.get("origin")
    if origin:
        try:
            host = _urlparse(origin).hostname or ""
        except Exception:
            host = ""
        if host not in _LOCAL_ORIGIN_HOSTS:
            await ws.close(code=1008)
            return
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
def _sse_stream_response(message: str, session_id: str, model: str):
    async def gen():
        import json as _js3
        import asyncio as _aio
        q: _aio.Queue = _aio.Queue()

        async def _on_delta(text: str):
            if text:
                await q.put({"delta": text})

        task = None
        sess = None
        try:
            # Fase 8: misma construccion estandar + streaming (pi).
            from src.core.provider_router import infer_hint
            slog = _session_log()
            # Reusar la sesion activa si se pidio y existe; si no, crear una.
            sess = slog.get_session(session_id) if session_id else None
            if sess is None:
                sess = slog.create_session(project="sse")
            _RUNNING[sess.id] = {"steer": [], "started": __import__("time").time()}
            task = _aio.create_task(_run_agent_once(
                message,
                session_id=sess.id,
                hint=infer_hint(message),
                model=None if model == "auto" else model,
                on_delta=_on_delta,
            ))
            while not task.done():
                try:
                    item = await _aio.wait_for(q.get(), timeout=0.25)
                    yield f"data: {_js3.dumps(item, ensure_ascii=False)}\n\n"
                except _aio.TimeoutError:
                    continue
            while not q.empty():
                yield f"data: {_js3.dumps(q.get_nowait(), ensure_ascii=False)}\n\n"
            result = await task
            reply = result.final_output or ("[sin respuesta]" if result.success else "[sin respuesta final del modelo]")
        except Exception as e:
            reply = f"Error: {e}"
        finally:
            # Si el cliente desconecta (GeneratorExit), cancelar el run para no fugarlo.
            if sess is not None:
                _RUNNING.pop(sess.id, None)
            if task is not None and not task.done():
                task.cancel()
                import contextlib as _ctx
                with _ctx.suppress(BaseException):
                    await task
        yield f"data: {_js3.dumps({'reply': reply}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/chat/stream")
async def sse_chat_get(message: str = "", model: str = "auto", session_id: str = ""):
    return _sse_stream_response(message, session_id, model)


@app.post("/api/chat/stream")
async def sse_chat_post(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    return _sse_stream_response(
        str(body.get("message", "")),
        str(body.get("session_id", "") or ""),
        str(body.get("model", "auto") or "auto"),
    )

@app.get("/{full_path:path}", include_in_schema=False)
async def _spa_fallback(full_path: str, request: Request):
        # Let /api already matched; this handles SPA routes
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        if not _UI_DIST.exists():
            raise HTTPException(status_code=404, detail="not found")
        # Contencion: solo servir archivos DENTRO de _UI_DIST (evita path traversal).
        base = _UI_DIST.resolve()
        try:
            candidate = (base / full_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(base):
                return FileResponse(str(candidate))
        except Exception:
            pass
        # Solo rutas SPA (sin extension) caen a index.html; assets faltantes -> 404 real.
        import os as _os
        if _os.path.splitext(full_path)[1]:
            raise HTTPException(status_code=404, detail="not found")
        # Fallback to index.html for client-side routing
        return FileResponse(str(base / "index.html"))


if not _UI_DIST.exists():
    @app.get("/", include_in_schema=False)
    async def _no_dist():
        return JSONResponse({"status": "ok", "dist": "ui/dist not built — run `npm run build` in ui/"})


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Nexus v3 harness backend")
    p.add_argument("command", nargs="?", default="serve", choices=["serve", "setup"],
                   help="serve: arranca el backend (defecto). setup: doctor de portabilidad a otra PC")
    p.add_argument("--port", type=int, default=8765, help="Port to listen on (0=random)")
    p.add_argument("--host", type=str, default="127.0.0.1")
    p.add_argument("--check", action="store_true", help="setup: solo audita, no instala nada")
    p.add_argument("--fix", action="store_true", help="setup: instala lo instalable (ollama pull)")
    return p.parse_args(argv)


def _ensure_local_brain() -> str:
    """Keep nexus-v3 self-contained: use its own ./brain by default.

    An ambient ``NEXUS_BRAIN`` may belong to another project on the same
    machine, which would silently link their brains. Only a v3-scoped override
    (``NEXUS_V3_BRAIN``) is honoured; otherwise the project brain wins.
    """
    import os as _os
    override = (_os.environ.get("NEXUS_V3_BRAIN") or "").strip()
    brain = override or str(_PROJECT_ROOT / "brain")
    _os.environ["NEXUS_V3_BRAIN"] = brain
    _os.environ["NEXUS_BRAIN"] = brain
    return brain


def main(argv=None):
    args = parse_args(argv)
    if args.command == "setup":
        from src.core.onboarding import run_setup
        # --check (o sin flags) = solo audita; --fix = instala lo instalable.
        return run_setup(fix=bool(args.fix and not args.check))
    brain = _ensure_local_brain()
    print(f"[nexus-v3] brain: {brain}")
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
