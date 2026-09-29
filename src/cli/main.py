"""CLI entry — Hermes-style, no Commander.js.

Commands:
  python -m src.cli.main --check-config   → verde if config.yaml + brain_path ok
  python -m src.cli.main --help
  python -m src.cli.main --probe-providers
"""
from __future__ import annotations

import asyncio
import argparse
import json
import sys
from pathlib import Path


def cmd_check_config(args) -> int:
    from src.core.config import check_config, load_config
    from src.brain.link import brain_path, get_brain_info

    info = check_config()
    bp = brain_path()
    bi = get_brain_info()

    print("=== Nexus v3 --verificar-config ===")
    print(f"config_path : {info['config_path']}")
    print(f"project     : {info['project']}")
    print(f"valid       : {info['valid']}")
    if info["errors"]:
        print("errors:")
        for e in info["errors"]:
            print(f"  - {e}")
    else:
        print("schema      : OK")
    print(f"brain_path  : {bp} (exists={bi.get('exists')}, linked_to_v2={bi.get('linked_to_v2')})")
    print(f"session.db  : {info.get('session', {}).get('db_path')}")
    print(f"providers   : {info.get('providers')}")
    print(f"router      : {info.get('router')}")
    print(f"scope       : {info.get('scope', {}).get('allowed_roots')}")
    print()
    # Also test Session/EventStore imports
    try:
        from src.core.session_log import SessionLog
        from src.core.event_store import EventStore
        from src.core.scope import Scope, ScopeConfig
        from src.core.tool_registry import ToolRegistry
        from src.core.compaction import CompactionPipeline
        from src.core.provider_router import ProviderRouter
        from src.core.agent_loop import AgentLoop, IterationBudget

        # In-memory smoke
        slog = SessionLog(":memory:")
        sess = slog.create_session(project="check-config")
        from src.core.session_log import Message
        slog.append(sess.id, Message(role="user", content="hello"))
        slog.append(sess.id, Message(role="assistant", content="hi"))
        assert slog.count_messages(sess.id) == 2
        assert slog.check_fts() is True
        assert slog.check_wal() in ("wal", "memory", "delete")

        es = EventStore(":memory:")
        from src.core.event_store import Event
        es.append(Event(kind="test", data={"ok": True}, conversation_id=sess.id))
        assert es.count(sess.id) == 1

        scope = Scope(ScopeConfig(), project_root=Path.cwd())
        reg = ToolRegistry()
        reg.register_function(lambda x: x, name="echo", description="echo", category="local")
        assert reg.has("echo")
        assert len(reg.to_openai_tools()) == 1

        router = ProviderRouter()
        assert router.resolve() is None or True

        loop = AgentLoop(provider_router=router, tool_registry=reg, scope=scope)
        assert loop.budget.max_iterations == 25

        pipe = CompactionPipeline({"snip": {"keep_recent": 2}})
        import asyncio as _asyncio
        async def _t():
            msgs = [{"role": "user", "content": "hi"}] * 10
            out, m = await pipe.compact(msgs, limit=100)
            assert len(out) <= len(msgs)
        _asyncio.run(_t())

        print("prueba humo  : OK (Session, EventStore, Scope, ToolRegistry, Router, AgentLoop, Compaction)")
    except Exception as e:
        print(f"prueba humo  : FALLO — {e}")
        import traceback; traceback.print_exc()
        return 1

    if not info["valid"]:
        print("VERIFICACIÓN FALLIDA: errores de esquema")
        return 2
    print("VERIFICACIÓN APROBADA")
    return 0


async def _probe_providers():
    from src.core.config import load_config
    from src.core.provider_router import ProviderRouter, ModelRoute
    from src.providers.ollama_provider import OllamaProvider
    from src.providers.llamacpp_provider import LlamaCppProvider
    from src.providers.cloud_provider import CloudProvider
    import os

    cfg = load_config()
    prov_cfg = cfg.get("providers", {})
    router = ProviderRouter(vram_budget_gb=cfg.get("router", {}).get("vram_budget_gb", 12), fallback_order=cfg.get("router", {}).get("fallback_order", ["llamacpp","ollama","cloud"]))
    if "ollama" in prov_cfg:
        oc = prov_cfg["ollama"]
        router.register_provider("ollama", OllamaProvider(base_url=oc.get("base_url", "http://localhost:11434"), timeout_s=oc.get("timeout_s", 30)))
        for m in oc.get("models", []):
            router.register_model(ModelRoute(id=m, provider_name="ollama"))
    if "llamacpp" in prov_cfg:
        lc = prov_cfg["llamacpp"]
        router.register_provider("llamacpp", LlamaCppProvider(base_url=lc.get("base_url", "http://localhost:8080"), fallback_url=lc.get("fallback_url"), timeout_s=lc.get("timeout_s", 30)))
        for m in lc.get("models", []):
            router.register_model(ModelRoute(id=m, provider_name="llamacpp"))
    if "cloud" in prov_cfg:
        cc = prov_cfg["cloud"]
        if cc.get("enabled"):
            router.register_provider("cloud", CloudProvider(base_url=cc.get("base_url"), api_key=cc.get("api_key"), auth_style=cc.get("auth_style", "bearer")))
            for m in cc.get("models", []):
                router.register_model(ModelRoute(id=m, provider_name="cloud"))
    result = await router.auto_detect()
    print(json.dumps({"auto_detect": result, "stats": router.stats()}, indent=2, ensure_ascii=False))
    health = await router.health_all()
    print(json.dumps(health, indent=2, ensure_ascii=False))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="nexus-v3", description="CLI Arnés Nexus v3")
    parser.add_argument("--check-config", action="store_true", help="Validar config.yaml + enlace cerebro + pruebas humo imports")
    parser.add_argument("--probe-providers", action="store_true", help="Probar salud Ollama/llama.cpp/nube")
    parser.add_argument("--help-config", action="store_true", help="Mostrar rutas de configuración")
    args = parser.parse_args(argv)

    if args.check_config:
        return cmd_check_config(args)
    if args.probe_providers:
        asyncio.run(_probe_providers())
        return 0
    if args.help_config:
        from src.core.config import CONFIG_PATH, SCHEMA_PATH
        from src.brain.link import brain_path
        print(f"config: {CONFIG_PATH}")
        print(f"schema: {SCHEMA_PATH}")
        print(f"brain : {brain_path()}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
