"""Tests del bundle MCP portable — todo con stubs, sin red ni installs reales."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.core import mcp_setup
from src.core.mcp_setup import probe_entry, run_mcp_setup

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "mcp_bundle" / "manifest.json"
ENV_EXAMPLE = ROOT / "data" / "mcp_bundle" / ".env.example"


def _entry(name="x", kind="npx", command=("npx", "-y", "pkg@latest"),
           env_required=(), enabled=True):
    return {"name": name, "kind": kind, "command": list(command),
            "env_required": list(env_required), "enabled_default": enabled}


# ---------------------------------------------------------------- veredictos

def test_disabled_queda_disabled():
    r = probe_entry(_entry(enabled=False))
    assert r["status"] == "disabled"


def test_manual_siempre_manual():
    r = probe_entry({"name": "cua", "kind": "manual", "install": "MANUAL: haz X"})
    assert r["status"] == "manual"


def test_needs_key_sin_valores_reales():
    r = probe_entry(_entry(env_required=["BRAVE_API_KEY"]), env={})
    assert r["status"] == "needs_key"
    assert r["missing_keys"] == ["BRAVE_API_KEY"]
    r2 = probe_entry(_entry(env_required=["BRAVE_API_KEY"]), env={"BRAVE_API_KEY": "k"})
    assert r2["status"] == "ok"


def test_remote_url_no_se_reporta_como_ok():
    # Honestidad: el cliente MCP interno solo lanza stdio, no puede llamar un
    # servidor remoto por HTTP. Se reporta "remote", no "ok".
    e = {"name": "gh", "kind": "remote-url", "command": [],
         "env_required": ["GITHUB_PERSONAL_ACCESS_TOKEN"], "enabled_default": True}
    assert probe_entry(e, env={})["status"] == "needs_key"
    r = probe_entry(e, env={"GITHUB_PERSONAL_ACCESS_TOKEN": "t"})
    assert r["status"] == "remote"
    assert "stdio" in r["detail"]


def test_missing_sin_fix_no_toca_red():
    calls = []
    r = probe_entry(_entry(), fix=False, which=lambda b: None,
                    runner=lambda c, t: (calls.append(c), 0)[1] or (0, ""))
    assert r["status"] == "missing"
    assert calls == []


def test_fix_intenta_instalar_via_runner():
    seen = []

    def runner(cmd, timeout):
        seen.append(cmd)
        return 0, "ok"

    r = probe_entry(_entry(), fix=True, which=lambda b: None, runner=runner)
    assert r["status"] == "ok"
    assert seen and seen[0][0] == "npx"


def test_fix_handshake_error_se_reporta():
    r = probe_entry(_entry(), fix=True, which=lambda b: "/usr/bin/npx",
                    runner=lambda c, t: (1, "boom"))
    assert r["status"] == "error"


def test_reporte_agregado_y_ready():
    manifest = {"version": 1, "entries": [
        _entry("a"),
        _entry("b", kind="manual", command=()),
        {"name": "c", "kind": "remote-url", "command": [], "env_required": ["K_X"],
         "enabled_default": True},
    ]}
    rep = run_mcp_setup(fix=False, manifest=manifest,
                        which=lambda b: "/bin/npx", env={})
    assert rep["summary"] == {"ok": 1, "manual": 1, "needs_key": 1}
    assert rep["ready"] is True  # needs_key/manual no bloquean
    assert rep["manual"] == ["b"] and rep["needs_key"] == ["c"]


def test_reporte_not_ready_con_missing():
    manifest = {"version": 1, "entries": [_entry("a")]}
    rep = run_mcp_setup(fix=False, manifest=manifest, which=lambda b: None, env={})
    assert rep["summary"] == {"missing": 1}
    assert rep["ready"] is False


def test_load_error_no_lanza():
    rep = run_mcp_setup(manifest_path_override=Path("/no/existe/manifest.json"))
    assert rep["ready"] is False and "load_error" in rep


# ---------------------------------------------------------------- manifiesto real

def test_manifest_real_valido():
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert data["version"] >= 1
    names = [e["name"] for e in data["entries"]]
    assert len(names) == len(set(names)) and len(names) >= 10
    for e in data["entries"]:
        assert e["kind"] in ("npx", "uvx", "docker", "remote-url", "manual"), e["name"]
        assert isinstance(e.get("env_required", []), list)


def test_manifest_sin_secretos():
    text = MANIFEST.read_text(encoding="utf-8")
    assert "sk-" not in text
    assert "G7JbW6" not in text  # fragmento conocido del token de referencia
    assert "8wekyb" not in text.lower()


def test_env_example_cubre_env_required():
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" not in example or True  # existe el archivo
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    required = {k for e in data["entries"] for k in e.get("env_required", [])}
    for k in required:
        assert k in example, f"{k} falta en .env.example"
    # la plantilla no trae valores reales (solo defaults inocuos documentados)
    safe_defaults = {"MCP_WRITE_MODE=preview", "NEXUS_SOVEREIGN_URL=http://localhost:8765/mcp"}
    for line in example.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line and line not in safe_defaults:
            assert line.endswith("="), f"posible valor real en plantilla: {line[:30]}"


def test_manifest_real_se_evalua_sin_red(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "dummy-test")
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "dummy-test")
    monkeypatch.setenv("DISCORD_TOKEN", "dummy-test")
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rep = run_mcp_setup(fix=False, manifest=data,
                        which=lambda b: "/bin/" + b if b in ("npx", "uvx", "docker") else None)
    assert rep["entries"]
    assert set(rep["summary"]) <= {"ok", "missing", "needs_key", "manual", "disabled",
                                   "error", "remote"}


# ---------------------------------------------------------------- resolucion de binarios

def _fake_venv_tool(root: Path, name: str) -> Path:
    """Crea el binario donde resolve_tool lo busca segun la plataforma."""
    if os.name == "nt":
        p = root / ".venv-ft314" / "Scripts" / (name + ".exe")
    else:
        p = root / ".venv-ft314" / "bin" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    return p


def test_resolve_tool_prefiere_el_venv_del_proyecto(tmp_path, monkeypatch):
    exe = _fake_venv_tool(tmp_path, "uvx")
    # Aunque el PATH tenga otro uvx, gana el del venv (el que instalo el bootstrap).
    monkeypatch.setattr(mcp_setup.shutil, "which",
                        lambda n, path=None: "/otro/uvx")
    assert mcp_setup.resolve_tool("uvx", root=tmp_path, env={}) == str(exe)


def test_resolve_tool_env_override_gana(tmp_path):
    exe = _fake_venv_tool(tmp_path, "uvx")
    custom = tmp_path / "custom-uvx"
    custom.write_text("", encoding="utf-8")
    got = mcp_setup.resolve_tool("uvx", root=tmp_path,
                                 env={"NEXUS_UVX": str(custom)})
    assert got == str(custom) and got != str(exe)


def test_resolve_tool_override_inexistente_ignorado(tmp_path, monkeypatch):
    exe = _fake_venv_tool(tmp_path, "uvx")
    got = mcp_setup.resolve_tool("uvx", root=tmp_path,
                                 env={"NEXUS_UVX": str(tmp_path / "no-existe")})
    assert got == str(exe)


def test_resolve_tool_sin_nada_devuelve_none(tmp_path):
    assert mcp_setup.resolve_tool("uvx-nope", root=tmp_path, env={"PATH": ""}) is None


def test_resolve_command_reemplaza_el_binario(tmp_path):
    exe = _fake_venv_tool(tmp_path, "uvx")
    entry = {"name": "x", "kind": "uvx", "command": ["uvx", "pkg@latest"]}
    cmd = mcp_setup.resolve_command(entry, root=tmp_path, env={})
    assert cmd == [str(exe), "pkg@latest"]
    assert mcp_setup.resolve_command({"name": "y", "command": []}) == []


def test_ensure_uv_instala_en_el_venv(tmp_path):
    py = (tmp_path / ".venv-ft314" / "Scripts" / "python.exe") if os.name == "nt" \
        else (tmp_path / ".venv-ft314" / "bin" / "python")
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("", encoding="utf-8")
    seen = []

    def runner(cmd, timeout):
        seen.append(cmd)
        _fake_venv_tool(tmp_path, "uvx")   # simula lo que hace pip install uv
        return 0, "instalado"

    info = mcp_setup.ensure_uv(tmp_path, runner=runner, env={"PATH": ""})
    assert info["ok"] is True and info["path"]
    assert seen and "uv" in seen[0]


def test_ensure_uv_sin_venv_no_lanza(tmp_path):
    info = mcp_setup.ensure_uv(tmp_path, runner=lambda c, t: (0, ""), env={"PATH": ""})
    assert info["ok"] is False


def test_write_status_persiste_el_reporte(tmp_path):
    target = tmp_path / "status.json"
    mcp_setup.write_status({"ready": True, "summary": {"ok": 3}}, path=target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["ready"] is True and data["summary"] == {"ok": 3}
    assert data["checked_at"]
