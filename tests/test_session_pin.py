"""Pinneo de la request: prompt y tools sobreviven al estado de hoy.

El defecto que fijan: reanudar re-armaba el prompt con el AGENTS.md, las skills
y el registry del momento. Un archivo editado o una tool nueva cambiaban el
contexto a mitad de conversación y el modelo veía otra cosa que la que venía
usando. Con el pin, el turno N+1 manda exactamente la request del turno N.
"""
from __future__ import annotations

import asyncio
import json

from src.core.agent_loop import AgentLoop, _tools_from_pin
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class CapturaProvider(BaseProvider):
    """Responde al toque y guarda qué recibió en cada turno."""

    def __init__(self):
        self.llamadas: list[dict] = []

    async def chat(self, messages, tools=None, model=None, **kw):
        self.llamadas.append({
            "system": messages[0].content or "",
            "tools": [str(((t or {}).get("function") or {}).get("name") or "") for t in (tools or [])],
            "roles": [m.role for m in messages],
        })
        return LLMResponse(content="listo", model="captura")

    async def health(self):
        return True

    async def list_models(self):
        return ["captura"]


def _registro(nombres=("echo", "otra")):
    reg = ToolRegistry()
    for nombre in nombres:
        def _fn(**kw):
            return "ok"
        reg.register_function(_fn, name=nombre, description=nombre,
                              category="local",
                              parameters={"type": "object", "properties": {},
                                          "additionalProperties": False})
    return reg


def _loop(slog, provider, reg):
    router = ProviderRouter()
    router.register_provider("mock", provider)
    router.register_model(ModelRoute(id="captura", provider_name="mock", tier=5))
    loop = AgentLoop(provider_router=router, tool_registry=reg,
                     budget=IterationBudget(max_iterations=3))
    loop.session_log = slog
    return loop


def _correr(loop, texto, **kw):
    async def _go():
        return await loop.run(task=texto, session_id=kw.pop("session_id"), hint="captura", **kw)
    return asyncio.run(_go())


def test_prompt_pinneado_sobrevive_a_un_agents_md_cambiado(monkeypatch):
    from src.core import agents_md

    slog = SessionLog(":memory:")
    prov = CapturaProvider()
    loop = _loop(slog, prov, _registro())
    try:
        sess = slog.create_session(project="pin")
        monkeypatch.setattr(agents_md, "system_block",
                            lambda paths, root: "\n\n[AGENTS] version A")
        _correr(loop, "primer pedido", session_id=sess.id, system_prompt="PROMPT ORIGINAL")
        primero = prov.llamadas[0]["system"]
        assert "PROMPT ORIGINAL" in primero and "version A" in primero

        # El proyecto cambia entre turnos: el pin tiene que ignorarlo.
        monkeypatch.setattr(agents_md, "system_block",
                            lambda paths, root: "\n\n[AGENTS] version B")
        _correr(loop, "segundo pedido", session_id=sess.id)
        segundo = prov.llamadas[1]["system"]
        assert segundo == primero, "el prompt pineado no puede re-armarse"
        assert "version B" not in segundo
    finally:
        slog.close()


def test_tools_pinneadas_filtran_el_registry_de_hoy(monkeypatch):
    from src.core import agents_md
    monkeypatch.setattr(agents_md, "system_block", lambda paths, root: "")

    slog = SessionLog(":memory:")
    prov = CapturaProvider()
    reg = _registro(("echo", "otra"))
    loop = _loop(slog, prov, reg)
    try:
        sess = slog.create_session(project="pin")
        _correr(loop, "primer pedido", session_id=sess.id, system_prompt="P")
        assert prov.llamadas[0]["tools"] == ["echo", "otra"]

        # Una tool nueva aparece en el registry: no se cuela en la sesión pineada.
        reg.register_function(lambda **kw: "ok", name="nueva", description="nueva",
                              category="local",
                              parameters={"type": "object", "properties": {},
                                          "additionalProperties": False})
        _correr(loop, "segundo pedido", session_id=sess.id)
        assert prov.llamadas[1]["tools"] == ["echo", "otra"]
        assert "nueva" not in prov.llamadas[1]["tools"]

        # Al limpiar el pin, la sesión vuelve a tomar el registry completo.
        assert slog.clear_pin(sess.id) is True
        _correr(loop, "tercer pedido", session_id=sess.id)
        assert "nueva" in prov.llamadas[2]["tools"]
    finally:
        slog.close()


def test_pin_se_guarda_y_se_limpia():
    slog = SessionLog(":memory:")
    try:
        sess = slog.create_session(project="pin")
        assert slog.get_pin(sess.id) is None

        pin = slog.pin_request(sess.id, "hola", [
            {"type": "function", "function": {"name": "echo", "parameters": {}}},
        ], model="m1")
        assert pin["names"] == ["echo"] and pin["model"] == "m1"
        guardado = slog.get_pin(sess.id)
        assert guardado["system_prompt"] == "hola"
        assert guardado["names"] == ["echo"]
        assert json.loads(slog.get_session(sess.id).tools_pinned)["hash"] == pin["hash"]

        # Sin force no se pisa el pin existente.
        assert slog.pin_request(sess.id, "otro", [])["system_prompt"] == "hola"
        # Con force sí.
        assert slog.pin_request(sess.id, "otro", [], force=True)["system_prompt"] == "otro"

        assert slog.clear_pin(sess.id) is True
        assert slog.get_pin(sess.id) is None
        assert slog.get_session(sess.id).to_dict()["prompt_pinned"] is False
    finally:
        slog.close()


def test_tools_from_pin_prefiere_el_schema_pineado():
    actual = [
        {"type": "function", "function": {"name": "echo", "description": "nuevo"}},
        {"type": "function", "function": {"name": "extra", "description": "solo hoy"}},
    ]
    pin = {"names": ["echo", "fantasma"],
           "schemas": [{"type": "function", "function": {"name": "echo", "description": "viejo"}}]}
    out = _tools_from_pin(actual, pin)
    assert [t["function"]["name"] for t in out] == ["echo"], "fantasma no tiene handler"
    assert out[0]["function"]["description"] == "viejo"

    # Un pin viejo sin schemas usa el schema actual con ese nombre.
    out = _tools_from_pin(actual, {"names": ["echo"]})
    assert out[0]["function"]["description"] == "nuevo"

    assert _tools_from_pin(actual, {"names": []}) is None
    assert _tools_from_pin(None, {"names": ["echo"]}) is None
