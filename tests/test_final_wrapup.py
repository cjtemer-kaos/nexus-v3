"""Cierre forzado (`_final_wrapup`): un turno cortado no puede quedar en placeholder.

Antes, si el modelo se quedaba iterando, el usuario recibia
`[presupuesto agotado antes de respuesta final]` (o `[doom-loop...]`) y nada mas:
el turno se habia pagado entero (25 iteraciones de un modelo local) y la
pregunta quedaba sin responder.

Ahora, al cortar, se hace UNA llamada mas con `tools=None` pidiendo un cierre en
texto con lo ya averiguado. Si esa llamada falla o viene vacia, se sintetiza el
ultimo resultado de herramienta. Los bucles conservan su diagnostico y siguen
siendo `success=False`.
"""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop, LLMMessage
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry


class WrapupProvider(BaseProvider):
    """Pide herramientas mientras las haya y responde en texto cuando no.

    `modo` decide como itera:
      - "varia": cada llamada usa argumentos distintos (no dispara repeat-stop)
      - "igual": siempre los mismos argumentos (dispara doom-loop)
      - "falla_cierre": revienta cuando el loop pide el cierre en texto
      - "vacio_cierre": devuelve texto vacio en el cierre
    """

    def __init__(self, modo: str = "varia", cierre: str = "Cierre: hice A, encontre B, falta C."):
        self.modo = modo
        self.cierre = cierre
        self.tool_calls = 0
        self.wrapup_calls = 0
        self.wrapup_tools: object = "no-llamado"

    async def chat(self, messages, tools=None, model=None, **kw):
        if tools:
            self.tool_calls += 1
            texto = "loop" if self.modo == "igual" else f"paso {self.tool_calls}"
            tc = ToolCall(id=f"c{self.tool_calls}", name="echo", arguments={"text": texto})
            return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="m")
        self.wrapup_calls += 1
        self.wrapup_tools = tools
        if self.modo == "falla_cierre":
            raise RuntimeError("el proveedor no acepta cierre sin herramientas")
        if self.modo == "vacio_cierre":
            return LLMResponse(content="   ", model="m")
        return LLMResponse(content=self.cierre, model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def _loop(provider, *, max_iterations=25, config=None, budget=None):
    reg = ToolRegistry()
    reg.register_function(lambda text: f"resultado de {text}", name="echo")
    router = ProviderRouter()
    router.register_provider("mock", provider)
    router.register_model(ModelRoute(id="mock-1", provider_name="mock"))
    return AgentLoop(
        provider_router=router,
        tool_registry=reg,
        budget=budget or IterationBudget(max_iterations=max_iterations),
        session_log=None,
        config=config,
    )


def _correr(provider, **kw):
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="wrapup")
            loop = _loop(provider, **kw)
            loop.session_log = slog
            return await loop.run(task="resolver la tarea", session_id=sess.id, hint="coding")
        finally:
            slog.close()

    return asyncio.run(run())


def test_presupuesto_agotado_devuelve_respuesta_no_placeholder():
    provider = WrapupProvider(modo="varia")
    res = _correr(provider, max_iterations=2)

    assert res.success, res.final_output
    assert "[presupuesto agotado" not in res.final_output
    assert res.final_output == provider.cierre
    # el cierre se pidio UNA sola vez y sin herramientas: no puede volver al bucle
    assert provider.wrapup_calls == 1
    assert provider.wrapup_tools is None
    assert any("cierre forzado sin herramientas" in (s.content or "") for s in res.steps)


def test_cierre_ve_lo_que_ya_habia_en_el_historial():
    """El cierre no inventa: el modelo recibe el historial con los resultados previos."""
    visto: list[str] = []

    class Espia(WrapupProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            if tools is None:
                visto.extend(str(m.content or "") for m in messages)
            return await super().chat(messages, tools=tools, model=model, **kw)

    provider = Espia(modo="varia")
    _correr(provider, max_iterations=2)

    assert provider.wrapup_calls == 1
    unido = "\n".join(visto)
    assert "resultado de paso 1" in unido
    assert "Se agoto el presupuesto" in unido


def test_doom_loop_conserva_diagnostico_y_suma_cierre():
    provider = WrapupProvider(modo="igual")
    res = _correr(provider, max_iterations=25)

    assert not res.success, "un bucle no puede declararse exitoso"
    assert "doom-loop" in res.final_output
    assert "sin respuesta final" in res.final_output, "el diagnostico no se pierde"
    assert provider.cierre in res.final_output
    assert provider.wrapup_tools is None


def test_bucle_por_herramienta_tambien_cierra():
    provider = WrapupProvider(modo="varia")
    config = {"agent_loop": {"anti_loop": {
        "repeat_stop": 99, "tool_warn": 2, "tool_stop": 3,
    }}}
    res = _correr(provider, max_iterations=25, config=config)

    assert not res.success
    assert "[bucle de herramienta" in res.final_output
    assert provider.cierre in res.final_output
    assert provider.wrapup_calls == 1


def test_si_el_cierre_falla_se_sintetiza_la_ultima_herramienta():
    provider = WrapupProvider(modo="falla_cierre")
    res = _correr(provider, max_iterations=2)

    assert provider.wrapup_calls == 1
    assert "[presupuesto agotado" not in res.final_output
    assert "respuesta sintetizada" in res.final_output
    assert "resultado de paso" in res.final_output


def test_cierre_vacio_cae_a_la_sintesis():
    provider = WrapupProvider(modo="vacio_cierre")
    res = _correr(provider, max_iterations=2)

    assert provider.wrapup_calls == 1
    assert "[presupuesto agotado" not in res.final_output
    assert "respuesta sintetizada" in res.final_output


def test_final_wrapup_sin_router_usa_sintesis():
    loop = _loop(WrapupProvider())
    loop.router = None
    messages = [
        LLMMessage(role="user", content="hace algo"),
        LLMMessage(role="tool", content="salida util de la herramienta"),
    ]
    pasos: list = []
    out = asyncio.run(loop._final_wrapup(messages, "coding", pasos, 3))

    assert "salida util de la herramienta" in out
    assert pasos == []


def test_final_wrapup_sin_nada_que_sintetizar_devuelve_vacio():
    """Sin resultados de herramienta no hay nada honesto que decir: devuelve ''."""
    loop = _loop(WrapupProvider())
    loop.router = None
    out = asyncio.run(loop._final_wrapup(
        [LLMMessage(role="user", content="hola")], "coding", [], 1))
    assert out == ""


class ReasoningProvider(BaseProvider):
    """Solo razonamiento: sin texto final y sin tool_calls (caso qwen3 thinking).

    El loop reintenta (`continue`) y esos reintentos consumen iteraciones sin
    pasar por OBSERVE, asi que el turno terminaba con `final_output` vacio.
    """

    def __init__(self, cierre: str = "Cierre: no llegue a la respuesta, esto es lo que se."):
        self.cierre = cierre
        self.wrapup_calls = 0
        self.wrapup_tools: object = "no-llamado"

    async def chat(self, messages, tools=None, model=None, **kw):
        if tools:
            return LLMResponse(content="", tool_calls=(), finish_reason="stop",
                               model="m", reasoning_content="pensando en la tarea...")
        self.wrapup_calls += 1
        self.wrapup_tools = tools
        return LLMResponse(content=self.cierre, model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def test_presupuesto_consumido_por_reintentos_de_razonamiento_no_deja_reply_vacio():
    provider = ReasoningProvider()
    res = _correr(provider, max_iterations=3)

    assert provider.wrapup_calls == 1, "el turno se corto y nadie pidio el cierre"
    assert res.final_output == provider.cierre
    assert res.success
    assert provider.wrapup_tools is None


def test_salida_por_iteraciones_sin_cierre_posible_no_queda_vacia():
    """Si el cierre tambien viene vacio, se devuelve un mensaje honesto, no ''."""

    class SinCierre(ReasoningProvider):
        async def chat(self, messages, tools=None, model=None, **kw):
            if tools is None:
                self.wrapup_calls += 1
                return LLMResponse(content="", model="m")
            return await super().chat(messages, tools=tools, model=model, **kw)

    provider = SinCierre()
    res = _correr(provider, max_iterations=3)

    assert provider.wrapup_calls == 1
    assert res.final_output, "un turno cortado no puede devolver cadena vacia"
    assert "presupuesto agotado" in res.final_output
    assert not res.success
