"""Triage de la prueba de humo del 2026-10-25.

Cinco hallazgos del arnés `scripts/_smoke_tools.py`, con su veredicto:

1. `sub_agent` con `tools: []` moría en `validate_args` ("debe ser string"):
   el schema pedía CSV y el modelo manda lista, que es lo natural.
2. `agent_run` / `gema_run` con un id inexistente caían al default y ejecutaban
   igual: delegación fail-open silenciosa.
3. `checkpoint_create` devolvía `files: 0` en su primer commit (diff-tree no
   lista el commit raíz sin `--root`). Los 17 s del humo no se reprodujeron:
   3 s en frío (antivirus/caché la primera vez).
4. `image_generate` devolvía "ERROR conectando a ComfyUI" como éxito porque
   ignoraba el returncode del script.
5. `code_search` con un archivo decía "Directory not found" (los modelos pasan
   el archivo que quieren mirar).

`roundtable` y `mcp_call` no se tocan: el primero documenta `timeout_s` como
tope *por voz* (y el juez tiene el suyo), el segundo falla con texto legible.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest


# --------------------------------------------------------------------------
# 1. sub_agent: tools acepta lista y CSV
# --------------------------------------------------------------------------
def _registry(tmp_path, monkeypatch):
    """Registry real con router stub: las validaciones ocurren antes del LLM."""
    from src.api.server import _build_registry
    from src.core.scope import Scope, ScopeConfig

    scope = Scope(ScopeConfig(allowed_roots=(str(tmp_path),), permission="full"),
                  project_root=tmp_path)
    cfg = {"scope": {"permission": "full"}}

    class _RouterStub:
        providers = []

        def resolve(self, *a, **k):
            return None

    return _build_registry(scope, "triage", cfg, router=_RouterStub(), helpers=True)


def test_sub_agent_declara_tools_como_array(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch)
    spec = reg.get("sub_agent").parameters["properties"]["tools"]
    assert spec["type"] == "array"
    assert spec["items"]["type"] == "string"


def test_sub_agent_acepta_lista_y_csv(tmp_path, monkeypatch):
    """`_coerce` convierte CSV → lista; la lista pasa tal cual. Ambos llegan al handler."""
    from src.core.tool_registry import validate_args

    reg = _registry(tmp_path, monkeypatch)
    schema = reg.get("sub_agent").parameters
    for entrada in ([], ["read_file"], ["read_file", "glob"]):
        out = validate_args("sub_agent", {"task": "x", "tools": entrada}, schema)
        assert out["tools"] == entrada
    out = validate_args("sub_agent", {"task": "x", "tools": "read_file, glob"}, schema)
    assert out["tools"] == ["read_file", "glob"]


# --------------------------------------------------------------------------
# 2. delegación fail-closed
# --------------------------------------------------------------------------
def test_gema_run_falla_cerrado_con_gema_inexistente(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as e:
        asyncio.run(reg.dispatch("gema_run", {"gema": "no-existe", "task": "hola"}, scope=None))
    assert "gema desconocida" in str(e.value)
    assert "code" in str(e.value)  # lista las válidas


def test_agent_run_falla_cerrado_con_agente_inexistente(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch)
    with pytest.raises(ValueError) as e:
        asyncio.run(reg.dispatch("agent_run", {"agent_id": "coder_local", "task": "hola"}, scope=None))
    assert "agente desconocido" in str(e.value)
    assert "default" in str(e.value)


# --------------------------------------------------------------------------
# 3. checkpoint: el commit raíz lista sus archivos
# --------------------------------------------------------------------------
def test_checkpoint_raiz_informa_los_archivos(tmp_path, monkeypatch):
    from src.core.git_checkpoints import create_git_checkpoint

    monkeypatch.setenv("NEXUS_GIT_CHECKPOINTS_DIR", str(tmp_path / "ckpts"))
    root = tmp_path / "repo"
    root.mkdir()
    (root / "uno.txt").write_text("hola\n", encoding="utf-8")
    (root / "dos.py").write_text("x = 1\n", encoding="utf-8")

    out = create_git_checkpoint("s", "raiz", root=root)
    assert out["ok"] is True
    assert out["changed"] is True
    assert out["files"] == 2

    same = create_git_checkpoint("s", "igual", root=root)
    assert same["changed"] is False


# --------------------------------------------------------------------------
# 4. image_generate propaga el fallo del script
# --------------------------------------------------------------------------
def test_image_generate_falla_si_el_script_sale_con_error(tmp_path, monkeypatch):
    script = tmp_path / "comfy_gen.py"
    script.write_text(
        "import sys\n"
        "print('ERROR conectando a ComfyUI: <urlopen error [WinError 10061]>')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("NEXUS_COMFY_SCRIPT", str(script))
    reg = _registry(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError) as e:
        asyncio.run(reg.dispatch("image_generate", {"prompt": "un cubo"}, scope=None))
    assert "exit 1" in str(e.value)
    assert "ComfyUI" in str(e.value)


# --------------------------------------------------------------------------
# 5. code_search apunta a un archivo
# --------------------------------------------------------------------------
def test_code_search_acepta_archivo(tmp_path):
    from src.tools.code_search import code_search

    f = tmp_path / "muestra.py"
    f.write_text("def dispatch(x):\n    return x\n", encoding="utf-8")
    out = asyncio.run(code_search("def dispatch", path=str(f), limit=5))
    assert out.get("success") is True
    assert out.get("count") == 1
    assert out["results"][0]["file"].replace("\\", "/").endswith("muestra.py")


def test_code_search_inexistente_sigue_fallando(tmp_path):
    from src.tools.code_search import code_search

    out = asyncio.run(code_search("x", path=str(tmp_path / "no" / "existe.py")))
    assert "Path not found" in out["error"]
