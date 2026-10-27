"""Formateo-in-the-loop: tras escribir, el resultado avisa el estado del formateo."""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse, ToolCall
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.scope import Scope, ScopeConfig
from src.core.tool_registry import ToolRegistry


class WriteThenFinal(BaseProvider):
    def __init__(self, path: str):
        self.path = path
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        if self.calls == 1:
            tc = ToolCall(id="c1", name="write_file", arguments={"path": self.path, "content": "x=1\n"})
            return LLMResponse(content="", tool_calls=(tc,), finish_reason="tool_calls", model="w")
        return LLMResponse(content="listo", model="w")

    async def health(self):
        return True

    async def list_models(self):
        return ["w"]


def _correr(tmp_path: Path, path: str):
    reg = ToolRegistry()

    def write_file(path: str, content: str = "") -> str:
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(content, encoding="utf-8")
        return f"escrito {destino}"

    reg.register_function(write_file, name="write_file", description="escribe un archivo")
    router = ProviderRouter()
    router.register_provider("mock", WriteThenFinal(path))
    router.register_model(ModelRoute(id="w", provider_name="mock", tier=3))
    scope = Scope(
        ScopeConfig(allowed_roots=(str(tmp_path),), permission="full"),
        project_root=tmp_path,
    )
    loop = AgentLoop(provider_router=router, tool_registry=reg, scope=scope,
                     budget=IterationBudget(max_iterations=4))
    loop.session_log = None
    return asyncio.run(loop.run(task="escribi el archivo", session_id="s-fmt"))


def _salida(res) -> str:
    return "\n".join(str(s.result.output or "") for s in res.steps if s.result)


def test_avisa_cuando_formateo(tmp_path: Path, monkeypatch):
    destino = tmp_path / "a.py"
    monkeypatch.setattr(
        "src.core.formatters.format_file",
        lambda path, cfg=None, root=None: {
            "status": "formatted", "file": str(path), "formatter": "ruff",
            "detail": "", "duration_ms": 5,
        },
    )
    res = _correr(tmp_path, str(destino))
    assert res.success
    assert "[formateado con ruff]" in _salida(res)


def test_avisa_cuando_falla_el_formateo(tmp_path: Path, monkeypatch):
    destino = tmp_path / "a.py"
    monkeypatch.setattr(
        "src.core.formatters.format_file",
        lambda path, cfg=None, root=None: {
            "status": "error", "file": str(path), "formatter": "ruff",
            "detail": "syntax error in line 3", "duration_ms": 5,
        },
    )
    res = _correr(tmp_path, str(destino))
    assert res.success
    salida = _salida(res)
    assert "[formateo falló (ruff)" in salida and "syntax error" in salida


def test_no_agrega_ruido_si_estaba_limpio(tmp_path: Path, monkeypatch):
    destino = tmp_path / "a.py"
    monkeypatch.setattr(
        "src.core.formatters.format_file",
        lambda path, cfg=None, root=None: {
            "status": "clean", "file": str(path), "formatter": "ruff",
            "detail": "", "duration_ms": 1,
        },
    )
    res = _correr(tmp_path, str(destino))
    assert res.success
    assert "formateado con" not in _salida(res)
    assert "formateo falló" not in _salida(res)


def test_un_formateador_roto_no_corta_el_turno(tmp_path: Path, monkeypatch):
    destino = tmp_path / "a.py"

    def _explota(path, cfg=None, root=None):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.core.formatters.format_file", _explota)
    res = _correr(tmp_path, str(destino))
    assert res.success
