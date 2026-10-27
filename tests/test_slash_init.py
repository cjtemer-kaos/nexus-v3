"""`/init`: escribe AGENTS.md, respeta el peldaño de permisos y no pisa sin force."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.core import slash_commands
from src.core.slash_commands import registry


def _run(cmd: str, ctx: dict):
    return asyncio.run(registry.execute(cmd, ctx))


def test_init_crea_agents_md(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    res = _run("/init", {"root": str(tmp_path)})
    destino = tmp_path / "AGENTS.md"
    assert res.ok, res.message
    assert res.data.get("action") == "init_created"
    assert destino.is_file()
    texto = destino.read_text(encoding="utf-8")
    assert "Stack detectado" in texto and "Python" in texto


def test_init_no_pisa_sin_force(tmp_path: Path):
    destino = tmp_path / "AGENTS.md"
    destino.write_text("# mio\n", encoding="utf-8")
    res = _run("/init", {"root": str(tmp_path)})
    assert not res.ok
    assert res.data.get("action") == "init_skipped"
    assert destino.read_text(encoding="utf-8") == "# mio\n"


def test_init_con_force_reemplaza(tmp_path: Path):
    destino = tmp_path / "AGENTS.md"
    destino.write_text("# mio\n", encoding="utf-8")
    res = _run("/init force", {"root": str(tmp_path)})
    assert res.ok, res.message
    assert res.data.get("action") == "init_created"
    assert res.data.get("replaced") is True
    assert "# mio" not in destino.read_text(encoding="utf-8")


def test_init_respeta_read_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "src.core.config.load_raw_config",
        lambda *a, **k: {"scope": {"permission": "read_only"}},
    )
    res = _run("/init", {"root": str(tmp_path)})
    assert not res.ok
    assert res.data.get("action") == "init_denied"
    assert not (tmp_path / "AGENTS.md").exists()


def test_init_es_aliasable_y_aparece_en_el_listado():
    nombres = {c.name for c in registry.list()}
    assert "init" in nombres
    cmd = registry.get("/agentsmd")
    assert cmd is not None and cmd.name == "init"
    assert cmd.to_dict()["origin"] == "builtin"
