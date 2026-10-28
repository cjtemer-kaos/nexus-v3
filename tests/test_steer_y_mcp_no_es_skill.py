"""La sesion 0c354614a682 fallo por tres cosas que se veian como "el agente no hace nada".

1. El usuario escribio "debes hacer funcionar rea en tu sistema, solucionalo" mientras
   el turno anterior seguia corriendo. La UI lo manda como STEER; el loop lo guardo en
   el historial y cerro el turno hablando de otra cosa: el pedido quedo sin respuesta.
2. El agente busco una SKILL llamada "rea", recibio "skill no encontrada" y concluyo que
   REA no estaba disponible. En realidad es un servidor MCP: arranca en ~5s con 138 tools.
3. `dir workspace/emule-next-gen` -> cmd lee "/emule-next-gen" como modificador y el
   agente culpaba a los permisos en vez de cambiar la barra.
"""
from __future__ import annotations

import asyncio
import json

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.tool_registry import ToolRegistry


class _ProviderConSteer(BaseProvider):
    def __init__(self):
        self.calls = 0
        self.vistos: list[list[str]] = []

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        self.vistos.append([m.content or "" for m in messages])
        return LLMResponse(content=f"respuesta {self.calls}", model="steer")

    async def health(self):
        return True

    async def list_models(self):
        return ["steer"]


def test_steer_que_llega_al_cerrar_el_turno_se_responde():
    async def run():
        prov = _ProviderConSteer()
        router = ProviderRouter()
        router.register_provider("mock", prov)
        router.register_model(ModelRoute(id="steer", provider_name="mock", tier=5))
        loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(),
                         budget=IterationBudget(max_iterations=5))
        loop.session_log = None
        n = {"steer": 0}

        def steer():
            n["steer"] += 1
            # 1ra llamada: drenaje normal del inicio del turno (nada pendiente).
            # 2da: el usuario escribio mientras el modelo redactaba la respuesta.
            return ["debes hacer funcionar rea"] if n["steer"] == 2 else []

        res = await loop.run(task="hace la tarea", session_id="s-steer", hint="simple", steer=steer)
        assert prov.calls == 2, f"el loop tenia que hacer otro turno para contestar el steer (hizo {prov.calls})"
        assert res.final_output == "respuesta 2"
        assert any("debes hacer funcionar rea" in c for c in prov.vistos[1]), \
            "el modelo tenia que ver el steer antes de cerrar"

    asyncio.run(run())


def _registry():
    from src.api.server import _build_registry, _build_router_from_config, _build_scope, _load_config_safe
    cfg = _load_config_safe()
    return cfg, _build_registry(_build_scope(cfg), "t", cfg,
                                router=_build_router_from_config(cfg))


def test_skill_que_en_realidad_es_un_mcp_no_dice_no_disponible():
    async def run():
        from src.api.server import _build_approval_context
        cfg, reg = _registry()
        appr = _build_approval_context(cfg, "t", source="chat", interactive=True)
        reg.approval = appr
        out = await reg.dispatch("skill", {"name": "rea"}, approval=appr, session_id="t")
        d = json.loads(out)
        assert "NO es una skill" in d["error"], d.get("error")
        assert d["mcp"]["server"] == "rea"
        assert "mcp_call" in d["mcp"]["como_usar"]

    asyncio.run(run())


def test_mcp_list_dice_como_se_usa_y_no_promete_conexion():
    async def run():
        from src.api.server import _build_approval_context
        cfg, reg = _registry()
        appr = _build_approval_context(cfg, "t", source="chat", interactive=True)
        reg.approval = appr
        out = await reg.dispatch("mcp_list", {"server": ""}, approval=appr, session_id="t")
        d = json.loads(out)
        assert "mcp_call" in d.get("como_usar", "")
        rea = next(s for s in d["servers"] if s["name"] == "rea")
        assert rea.get("estado"), "cada servidor tiene que traer su estado real"
        assert "enabled_default" in rea

    asyncio.run(run())


def test_pista_cuando_cmd_lee_la_ruta_como_modificador():
    from src.api.server import _cmd_shell_hint
    out = 'El modificador no es v\u00e1lido: "emule-next-gen"".'
    pista = _cmd_shell_hint("dir workspace/emule-next-gen", out)
    assert "barra invertida" in pista
    assert "list_dir" in pista
    # Un error de modificador sin barra en el comando no inventa esa pista.
    assert _cmd_shell_hint("dir C:\\x", out) == ""


def test_mcp_list_compacto_y_con_filtro():
    """`rea` son 138 tools y 569 KB de schemas: el modelo no podia leerlos, asi que
    inventaba nombres ("analyze") y el run abortaba. Por defecto va nombre + resumen."""
    async def run():
        from src.api.server import _build_approval_context
        cfg, reg = _registry()
        appr = _build_approval_context(cfg, "t", source="chat", interactive=True)
        reg.approval = appr

        out = await reg.dispatch("mcp_list", {"server": "rea"}, approval=appr, session_id="t")
        d = json.loads(out)
        assert len(out) < 20000, f"el listado compacto no puede pesar {len(out)} chars"
        assert d["total"] >= 50, d["total"]
        assert all("name" in t for t in d["tools"])
        assert "mcp_call" in d["como_usar"]

        out2 = await reg.dispatch("mcp_list", {"server": "rea", "filter": "annotate"},
                                  approval=appr, session_id="t")
        d2 = json.loads(out2)
        assert 0 < d2["mostradas"] < d2["total"], d2["mostradas"]
        assert len(out2) < 3000, len(out2)

        out3 = await reg.dispatch("mcp_list", {"server": "rea", "filter": "annotate_native_function",
                                               "full": True}, approval=appr, session_id="t")
        d3 = json.loads(out3)
        assert d3["mostradas"] == 1
        # full=True trae el schema de argumentos, no solo el nombre.
        assert (d3["tools"][0].get("input_schema") or {}).get("properties")

    asyncio.run(run())


def test_mcp_call_con_tool_inexistente_devuelve_la_lista_y_no_aborta():
    """En la corrida real el modelo llamo mcp_call(tool='analyze') y el run murio con
    "[abortado: mcp_call -> Tool analyze not found]". Un nombre mal escrito tiene que
    devolver available_tools para que el agente corrija y siga."""
    async def run():
        from src.api.server import _build_approval_context
        cfg, reg = _registry()
        appr = _build_approval_context(cfg, "t", source="chat", interactive=True)
        reg.approval = appr
        out = await reg.dispatch("mcp_call",
                                 {"server": "rea", "tool": "analyze", "arguments": {}},
                                 approval=appr, session_id="t")
        d = json.loads(out)
        assert d["isError"] is True
        assert d["available_tools"], "sin la lista el modelo vuelve a adivinar"
        assert "analyze" not in d["available_tools"]

    asyncio.run(run())


def test_tool_not_found_no_es_error_fatal():
    """El clasificador mandaba "Tool X not found" a ABORT (matchea `not found`)."""
    from src.core.error_classifier import ErrorAction, classify_error
    assert classify_error("mcp_call", "Tool analyze not found") == ErrorAction.REPLAN
    assert classify_error("mcp_call", "Unknown tool: analyze") == ErrorAction.REPLAN
    # Un archivo inexistente sigue siendo fatal: no se reintenta a ciegas.
    assert classify_error("read_file", "no such file: C:\\x") == ErrorAction.ABORT
