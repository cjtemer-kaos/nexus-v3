"""Agent registry — ayudantes con LLM independiente."""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.core.agent_registry import AgentRegistry, normalize_agent_id


def _reg(td: str) -> AgentRegistry:
    return AgentRegistry(Path(td) / "agents" / "registry.json")


def test_default_agent_present_and_protected():
    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        ids = [a["id"] for a in r.list()]
        assert "default" in ids
        try:
            r.delete("default")
            raise AssertionError("default no deberia borrarse")
        except ValueError:
            pass


def test_create_update_delete_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        a = r.create(name="Coder Local", model="qwen2.5-coder:7b", provider="ollama",
                     system_prompt="Eres un ingeniero senior.", emoji="🛠️")
        assert a["id"] == "coder-local"
        assert a["model"] == "qwen2.5-coder:7b"
        upd = r.update("coder-local", {"model": "qwen3.5-9b", "toolsets": "read_file,exec"})
        assert upd["model"] == "qwen3.5-9b"
        assert upd["toolsets"] == ["read_file", "exec"]
        assert r.get("coder-local") is not None
        r.delete("coder-local")
        assert r.get("coder-local") is None


def test_resolve_per_assistant():
    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        r.create(name="critic", model="deepseek-r1:8b", system_prompt="Revisa con rigor.")
        got = r.resolve("critic")
        assert got["model"] == "deepseek-r1:8b"
        assert got["system_prompt"] == "Revisa con rigor."
        assert r.resolve(None)["id"] == "default"
        assert r.resolve("no-existe")["id"] == "default"


def test_invalid_and_duplicate_names():
    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        for bad in ("", "   ", "!!!"):
            try:
                r.create(name=bad)
                raise AssertionError(f"deberia rechazar {bad!r}")
            except ValueError:
                pass
        r.create(name="dup")
        try:
            r.create(name="Dup")
            raise AssertionError("duplicado no rechazado")
        except ValueError:
            pass


def test_persistence_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        r1 = _reg(td)
        r1.create(name="persist", model="gemma4")
        r2 = _reg(td)
        assert r2.get("persist")["model"] == "gemma4"


def test_normalize_agent_id():
    assert normalize_agent_id("PC1 Coder") == "pc1-coder"
    assert normalize_agent_id("  Mi Agente!! ") == "mi-agente"
    assert normalize_agent_id("--x--") == "x"


def test_workspace_isolation():
    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        a = r.create(name="isolado")
        ws = Path(td) / "agents" / a["id"] / "workspace"
        assert ws.is_dir()
        assert Path(r.resolve(a["id"])["workspace_abs"]) == ws.resolve()
        assert Path(r.resolve("default")["workspace_abs"]).is_dir()


def test_system_prompt_not_truncated():
    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        long_prompt = "Eres un ingeniero senior. " * 40  # > 200 chars
        a = r.create(name="largo", system_prompt=long_prompt)
        assert len(a["system_prompt"]) == len(long_prompt.strip())
        assert r.resolve("largo")["system_prompt"] == long_prompt.strip()


def test_gema_field_and_catalog():
    from src.core.gema_profiles import list_gemas, hint_for_gema, system_prompt_for_gema

    with tempfile.TemporaryDirectory() as td:
        r = _reg(td)
        r.create(name="coder", gema="code")
        assert r.resolve("coder")["gema"] == "code"

    gemas = list_gemas()
    assert len(gemas) >= 20
    assert any(g["name"] == "code" for g in gemas)
    assert hint_for_gema("code") == "coding"
    assert "ingeniero" in system_prompt_for_gema("code").lower()
    assert hint_for_gema("no-existe") is None
