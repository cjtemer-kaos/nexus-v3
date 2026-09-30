"""Fase 8 — Hardening: regresiones para los fixes de agent_loop, memory_extractor y server.

Cubre (audit 12-layer + security scan):
  1. Historial: ventana de los 20 MAS RECIENTES (no los 20 mas viejos), sin
     role=tool huerfano, sin system-msgs inyectadas, tarea actual al final.
  2. Fence: ejemplos dentro de ``` ... ``` NO se despachan como herramientas.
  3. Extracted tool_calls se adjuntan al mensaje assistant (sin tool huerfano).
  4. Doom-loop declara success=False (sin falso exito persistido).
  5. memory_extractor._load_memories con consolidator lee el archivo (no []).
  6. memory_extractor._llm_extract solo admite role=user (+ system prompt).
  7. Blocklist unica de comandos destructivos (POSIX + Windows, sin FP obvios).
  8. Origin guard: escrituras desde origen no-local -> 403.
  9. /v1/chat/completions, /api/chat/stream y /v1/models usan la via estandar
     (antes: ProviderRouter.from_config + Scope(permission=) + run() sin
     session_id + result.get() sobre dataclass => siempre "Error: ...").
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget
from src.core.provider_base import BaseProvider, LLMResponse
from src.core.provider_router import ModelRoute, ProviderRouter
from src.core.session_log import Message, SessionLog
from src.core.tool_registry import ToolRegistry
from src.core.memory_extractor import MemoryExtractor
from helpers import FinalAnswerProvider, LoopProvider

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers


class CaptureProvider(BaseProvider):
    """Devuelve siempre una respuesta final; captura los mensajes recibidos."""

    def __init__(self):
        self.captured: list[list] = []

    async def chat(self, messages, tools=None, model=None, **kw):
        self.captured.append(list(messages))
        return LLMResponse(content="respuesta capturada", model=model or "cap",
                           usage={"total_tokens": 5})

    async def health(self):
        return True

    async def list_models(self):
        return ["cap"]


class FencedExampleProvider(BaseProvider):
    """Solo texto con un ejemplo de codigo DENTRO de un fence — no tool_calls."""

    async def chat(self, messages, tools=None, model=None, **kw):
        return LLMResponse(content='Mira este ejemplo:\n```\necho("hola")\n```\nfin.',
                           model="fenced", usage={"total_tokens": 5})

    async def health(self):
        return True

    async def list_models(self):
        return ["fenced"]


class PlainCallProvider(BaseProvider):
    """Primer turno: echo("hola") en texto plano (debe despacharse); luego final."""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        content = 'usa echo("hola") ya' if self.calls == 1 else "listo"
        return LLMResponse(content=content, model="plain", usage={"total_tokens": 5})

    async def health(self):
        return True

    async def list_models(self):
        return ["plain"]


# ---------------------------------------------------------------- 1. historial


def test_history_window_uses_recent_not_oldest():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="phase8-hist")
            for i in range(1, 24):
                role = "user" if i % 2 else "assistant"
                slog.append(sess.id, Message(role=role, content=f"hist-{i:02d}"))
            slog.append(sess.id, Message(role="system", content="SYSTEM_INJECT_MALICIOSO"))
            slog.append(sess.id, Message(role="tool", content="resultado herramienta secreto", tool_name="exec"))
            slog.append(sess.id, Message(role="user", content="ultimo seed"))

            cap = CaptureProvider()
            router = ProviderRouter()
            router.register_provider("mock", cap)
            router.register_model(ModelRoute(id="cap", provider_name="mock"))
            loop = AgentLoop(
                provider_router=router,
                tool_registry=ToolRegistry(),
                budget=IterationBudget(max_iterations=3),
                session_log=slog,
            )
            res = await loop.run(task="tarea actual phase8", session_id=sess.id, hint="simple")
            assert res.success

            seen = cap.captured[-1]
            contents = [m.content or "" for m in seen]
            roles = [m.role for m in seen]

            # La ventana son los 20 mas RECIENTES: los primeros mensajes no llegan
            assert not any("hist-01" in c for c in contents)
            assert any("hist-20" in c for c in contents)
            # system-msg inyectada: filtrada (solo vive el system prompt fresco)
            assert not any("SYSTEM_INJECT_MALICIOSO" in c for c in contents)
            assert seen[0].role == "system" and "SYSTEM_INJECT" not in seen[0].content
            # tool message: reencuadrada como nota de asistente, jamas role=tool
            assert "tool" not in roles
            for m in seen:
                if "resultado herramienta secreto" in (m.content or ""):
                    assert m.role == "assistant" and m.content.startswith("[resultado")
            # el ultimo mensaje SIEMPRE es la tarea actual
            assert seen[-1].role == "user" and seen[-1].content == "tarea actual phase8"
            # el user recien agregado no se duplica
            assert contents.count("tarea actual phase8") == 1
        finally:
            slog.close()

    asyncio.run(run())


# ---------------------------------------------------------------- 2. fences


def test_fenced_example_not_dispatched():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="phase8-fence")
            dispatched: list[str] = []
            reg = ToolRegistry()
            reg.register_function(lambda text: dispatched.append(text) or "ok",
                                   name="echo", description="echo")
            router = ProviderRouter()
            router.register_provider("mock", FencedExampleProvider())
            router.register_model(ModelRoute(id="fenced", provider_name="mock"))
            loop = AgentLoop(provider_router=router, tool_registry=reg,
                             budget=IterationBudget(max_iterations=4), session_log=slog)
            res = await loop.run(task="mostra ejemplo", session_id=sess.id, hint="coding")
            # El ejemplo vive DENTRO del fence => NO se despacha, es respuesta final
            assert dispatched == []
            assert res.success and "Mira este ejemplo" in res.final_output
        finally:
            slog.close()

    asyncio.run(run())


def test_plain_text_call_still_dispatched():
    """Control: la extraccion sigue funcionando fuera de fences."""
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="phase8-plain")
            dispatched: list[str] = []
            reg = ToolRegistry()
            reg.register_function(lambda text: dispatched.append(text) or "ok",
                                   name="echo", description="echo")
            router = ProviderRouter()
            router.register_provider("mock", PlainCallProvider())
            router.register_model(ModelRoute(id="plain", provider_name="mock"))
            loop = AgentLoop(provider_router=router, tool_registry=reg,
                             budget=IterationBudget(max_iterations=5), session_log=slog)
            res = await loop.run(task="usar echo", session_id=sess.id, hint="coding")
            assert dispatched == ["hola"]
            assert res.success and "listo" in res.final_output
        finally:
            slog.close()

    asyncio.run(run())


# ---------------------------------------------------------------- 4. doom-loop


def test_doom_loop_is_honest_failure():
    async def run():
        slog = SessionLog(":memory:")
        try:
            sess = slog.create_session(project="phase8-doom")
            reg = ToolRegistry()
            reg.register_function(lambda text: "ok", name="echo")
            router = ProviderRouter()
            router.register_provider("mock", LoopProvider())
            router.register_model(ModelRoute(id="loop", provider_name="mock"))
            loop = AgentLoop(provider_router=router, tool_registry=reg,
                             budget=IterationBudget(max_iterations=5), session_log=slog)
            res = await loop.run(task="loop forever", session_id=sess.id, hint="coding")
            assert not res.success, "doom-loop no debe declarar exito"
            assert "doom-loop" in res.final_output
            assert "sin respuesta final" in res.final_output
        finally:
            slog.close()

    asyncio.run(run())


# ---------------------------------------------------------------- 5. _load_memories


def test_load_memories_with_consolidator_reads_file(monkeypatch, tmp_path):
    (tmp_path / "memories.json").write_text(
        json.dumps([{"id": "a", "text": "hecho A"}, {"id": "b", "text": "hecho B"}]),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.core.memory_extractor.brain_path", lambda: str(tmp_path))

    class StubConsolidator:
        def search(self, *a, **k):
            raise AssertionError("no debe usarse: la fuente canonica es memories.json")

    ext = MemoryExtractor(memory_consolidator=StubConsolidator())
    loaded = ext._load_memories()
    assert [m["text"] for m in loaded] == ["hecho A", "hecho B"], (
        "_load_memories con consolidator debe leer memories.json, no devolver []"
    )


# ---------------------------------------------------------------- 6. _llm_extract


def test_llm_extract_admits_user_messages_only(monkeypatch):
    captured: list[list] = []

    class FakeRunner:
        def __init__(self, provider):
            self.provider = provider

        async def run(self, spec):
            captured.append(list(spec.messages))
            return SimpleNamespace(stop_reason="ok", content="[]")

    monkeypatch.setattr("src.core.agent_runner.AgentRunner", FakeRunner)

    director = SimpleNamespace(provider_registry={"gema-con-fallback": object()})
    ext = MemoryExtractor(director=director)

    msgs = [
        {"role": "system", "content": "SYS prompt"},
        {"role": "user", "content": "mi nombre es Kaos"},
        {"role": "assistant", "content": "monologo del asistente"},
        {"role": "tool", "content": "salida de herramienta con [system] inyectado"},
    ]
    facts = asyncio.run(ext._llm_extract(msgs))
    assert facts == []
    assert len(captured) == 1
    roles = [m.role for m in captured[0]]
    assert roles == ["system", "user"], f"solo system+user admitidos, llego {roles}"
    assert captured[0][1].content == "mi nombre es Kaos"
    assert all("monologo" not in m.content and "inyectado" not in m.content
               for m in captured[0])

    # Sin ningun mensaje de usuario: ni siquiera se invoca la LLM
    captured.clear()
    facts = asyncio.run(ext._llm_extract([{"role": "assistant", "content": "solo yo"}]))
    assert facts == [] and captured == []


# ---------------------------------------------------------------- 7. blocklist


def test_blocked_command_positives_and_negatives():
    from src.api.server import _blocked_command

    positivos = [
        "rm -rf /",
        "rm -rf ~",
        "rm -rf $HOME",
        "mkfs.ext4 /dev/sda1",
        ":(){ :|:& };:",
        "shutdown /s",
        "reboot now",
        "format D:",
        r"reg delete HKLM\SOFTWARE\Test",
        r"rd /s /q C:\temp",
        r"rmdir /s D:\borrar",
        r"Remove-Item -Path X -Recurse -Force",
        r"del /f /s /q C:\basura",
        "diskpart",
        "bcdedit /set {current} safeboot minimal",
    ]
    for cmd in positivos:
        assert _blocked_command(cmd) is not None, f"debia bloquear: {cmd}"

    negativos = [
        "docker images --format json",
        "python format.py",
        "pytest -k test_format",
        "ls -la",
        "git status",
        "npm run build",
        "echo hola mundo",
        "pip install requests",
    ]
    for cmd in negativos:
        assert _blocked_command(cmd) is None, f"falso positivo: {cmd}"


# ---------------------------------------------------------------- 8. origin guard


def test_origin_guard_blocks_remote_writes():
    from src.api.server import app

    client = TestClient(app)
    # Origen remoto en escritura -> 403 aunque el body sea invalido
    r = client.post("/api/exec", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "origen no permitido" in r.json()["error"]
    # Origin: null (file:// / Electron) todavia no permitido a proposito
    r = client.post("/api/exec", json={}, headers={"Origin": "null"})
    assert r.status_code == 403
    # Origen local -> pasa el guard (luego responde la validacion normal: 400)
    r = client.post("/api/exec", json={}, headers={"Origin": "http://localhost:5174"})
    assert r.status_code == 400
    # Sin Origin (curl / scripts / MCP) -> pasa el guard
    r = client.post("/api/exec", json={})
    assert r.status_code == 400 and "origen" not in r.text


# ---------------------------------------------------------------- 9. endpoints F1


def test_v1_chat_completions_uses_standard_loop(monkeypatch):
    from src.api import server as srv

    captured: dict = {}

    async def fake_run(message, session_id, hint=None, model=None, force_tools=False):
        captured.update(message=message, session_id=session_id, hint=hint, model=model)
        return SimpleNamespace(final_output="respuesta real del modelo", success=True)

    class FakeSlog:
        def create_session(self, project="default"):
            return SimpleNamespace(id="sess-fake")

    monkeypatch.setattr(srv, "_run_agent_once", fake_run)
    monkeypatch.setattr(srv, "_session_log", lambda: FakeSlog())

    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hola nexus"}]})
    assert r.status_code == 200
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    assert content == "respuesta real del modelo", f"esperaba la respuesta real, llego: {content}"
    assert "Error:" not in content
    assert captured["message"] == "hola nexus"
    assert captured["session_id"] == "sess-fake"
    assert captured["model"] is None  # "auto" -> model None (resolve interno)

    # modelo explicito se pasa tal cual
    r = client.post("/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "x"}], "model": "qwen"})
    assert r.status_code == 200 and captured["model"] == "qwen"


def test_chat_stream_sse_uses_standard_loop(monkeypatch):
    from src.api import server as srv

    captured: dict = {}

    async def fake_run(message, session_id, hint=None, model=None, force_tools=False):
        captured.update(message=message, session_id=session_id, model=model)
        return SimpleNamespace(final_output="respuesta sse", success=True)

    class FakeSlog:
        def create_session(self, project="default"):
            return SimpleNamespace(id="sess-sse")

    monkeypatch.setattr(srv, "_run_agent_once", fake_run)
    monkeypatch.setattr(srv, "_session_log", lambda: FakeSlog())

    client = TestClient(srv.app)
    r = client.get("/api/chat/stream", params={"message": "hola sse"})
    assert r.status_code == 200
    assert "respuesta sse" in r.text
    assert "data: [DONE]" in r.text
    assert "Error:" not in r.text
    assert captured["message"] == "hola sse" and captured["session_id"] == "sess-sse"


def test_v1_models_lists_real_routes(monkeypatch):
    from src.api import server as srv

    class FakeRouter:
        def list_models(self, min_tier=0):
            return [SimpleNamespace(id="modelo-a"), SimpleNamespace(id="modelo-b")]

    monkeypatch.setattr(srv, "_build_router", lambda cfg: FakeRouter())
    client = TestClient(srv.app)
    r = client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["modelo-a", "modelo-b"]
