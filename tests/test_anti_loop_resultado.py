"""Anti-bucle por salida repetida: la misma herramienta puede cambiar de args y
seguir sin avanzar.

Caso real (sesión `hola`, 2026-10-28): el modelo pidió
`systeminfo | findstr /c:"processor"`, luego con `/c:"system type"`, luego con
ambos, y cada variante devolvía la MISMA línea ("Tipo de compilación del sistema
operativo: Multiprocessor Free"). El guard viejo contaba por nombre de
herramienta y cortaba recién a las 10, así que el turno se quemó repitiendo. La
señal correcta es el resultado idéntico, no los argumentos.
"""
from __future__ import annotations

import asyncio

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import SessionLog
from src.core.tool_registry import ToolRegistry

SALIDA_FIJA = "Tipo de compilación del sistema operativo: Multiprocessor Free"


class ExecProvider(BaseProvider):
    """Pide `exec` con argumentos distintos en cada vuelta."""

    def __init__(self):
        self.vistos: list[list[str]] = []

    async def chat(self, messages, tools=None, model=None, **kw):
        self.vistos.append([str(m.content or "") for m in messages])
        if tools is None:
            return LLMResponse(content="cierre sintético", model="m")
        n = len(self.vistos)
        return LLMResponse(
            content="", tool_calls=(
                ToolCall(id=f"c{n}", name="exec",
                         arguments={"command": f"systeminfo | findstr /c:\"v{n}\""}),),
            finish_reason="tool_calls", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


class Salida:
    def __init__(self, fija: bool):
        self.fija = fija
        self.n = 0

    def __call__(self, command: str = "") -> str:
        self.n += 1
        return SALIDA_FIJA if self.fija else f"{SALIDA_FIJA} #{self.n}"


def _correr(salida: Salida, iteraciones: int = 6):
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="anti-loop-resultado")
            reg = ToolRegistry()
            reg.register_function(salida, name="exec")
            router = ProviderRouter()
            router.register_provider("mock", ExecProvider())
            router.register_model(ModelRoute(id="mock-1", provider_name="mock"))
            loop = AgentLoop(
                provider_router=router, tool_registry=reg, session_log=slog,
                budget=IterationBudget(max_iterations=iteraciones),
                config={"agent_loop": {"anti_loop": {
                    "repeat_stop": 99, "repeat_warn": [99], "tool_warn": 99, "tool_stop": 99}}},
            )
            res = await loop.run(task="averiguá el procesador", session_id=sess.id, hint="coding")
            msgs = slog.get_messages(sess.id)
            return res, msgs
        finally:
            slog.close()

    return asyncio.run(run())


def test_salida_identica_corta_el_turno_a_la_tercera():
    res, msgs = _correr(Salida(fija=True))
    assert "bucle de herramienta" in res.final_output
    assert "mismo resultado" in res.final_output
    # Cortó en la tercera repetición, no en la décima: sólo tres resultados.
    assert len([m for m in msgs if m.role == "tool"]) == 3


def test_el_cierre_explica_que_se_hizo_y_no_queda_vacio():
    res, _ = _correr(Salida(fija=True))
    # El usuario tiene que recibir algo más que el tag del corte.
    assert "cierre sintético" in res.final_output
    assert res.success is False


def test_el_corte_queda_persistido_en_la_sesion():
    res, msgs = _correr(Salida(fija=True))
    finales = [m for m in msgs if m.role == "assistant" and "bucle de herramienta" in (m.content or "")]
    assert finales, "el cierre del corte no se persiste: al reabrir la sesión no hay explicación"
    assert "cierre sintético" in finales[-1].content


def test_salida_distinta_no_dispara_el_corte_por_resultado():
    res, _ = _correr(Salida(fija=False), iteraciones=4)
    assert "mismo resultado" not in res.final_output


class EditYExecProvider(BaseProvider):
    """Alterna verificación (`exec`) con escritura (`edit`), como un turno real."""

    async def chat(self, messages, tools=None, model=None, **kw):
        if tools is None:
            return LLMResponse(content="listo", model="m")
        n = len([m for m in messages if getattr(m, "role", "") == "tool"])
        if n % 2 == 0:
            return LLMResponse(
                content="", tool_calls=(
                    ToolCall(id=f"e{n}", name="edit",
                             arguments={"path": "x.py", "old": "a", "new": "b"}),),
                finish_reason="tool_calls", model="m")
        return LLMResponse(
            content="", tool_calls=(
                ToolCall(id=f"x{n}", name="exec",
                         arguments={"command": "pytest -q"}),),
            finish_reason="tool_calls", model="m")

    async def health(self):
        return True

    async def list_models(self):
        return ["m"]


def test_verificar_despues_de_escribir_no_es_bucle():
    """La misma salida de `pytest` tras una edición es regresión, no bucle.

    Medido en la corrida de eMule: 122 vueltas útiles cortadas por el tercer
    `pytest` idéntico, con ediciones entre las tres corridas.
    """

    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="anti-loop-edit-exec")
            reg = ToolRegistry()
            reg.register_function(lambda command="": SALIDA_FIJA, name="exec")
            reg.register_function(
                lambda path="", old="", new="": f"editado {path}", name="edit")
            router = ProviderRouter()
            router.register_provider("mock", EditYExecProvider())
            router.register_model(ModelRoute(id="mock-1", provider_name="mock"))
            loop = AgentLoop(
                provider_router=router, tool_registry=reg, session_log=slog,
                budget=IterationBudget(max_iterations=12),
                config={"agent_loop": {"anti_loop": {
                    "repeat_stop": 99, "repeat_warn": [99], "tool_warn": 99, "tool_stop": 99}}},
            )
            res = await loop.run(task="arreglá y verificá", session_id=sess.id, hint="coding")
            return res, slog.get_messages(sess.id)
        finally:
            slog.close()

    res, msgs = asyncio.run(run())
    assert "bucle de herramienta" not in (res.final_output or "")
    ejecuciones = [m for m in msgs if m.role == "tool" and m.tool_name == "exec"]
    assert len(ejecuciones) >= 3, "el test no llegó a repetir la verificación"
