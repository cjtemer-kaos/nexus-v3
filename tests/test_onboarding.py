"""Setup doctor de portabilidad: detecciones con mocks + politica por perfiles + pulls simulados."""
from __future__ import annotations

import io
import json
import subprocess
import urllib.request
from pathlib import Path
from unittest import mock

from src.core import onboarding as ob


def _tags_response(names):
    payload = json.dumps({"models": [{"name": n} for n in names]}).encode()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload

    return _Resp()


# --- Ollama ---------------------------------------------------------------

def test_check_ollama_ok_lists_models():
    with mock.patch.object(urllib.request, "urlopen", return_value=_tags_response(["qwen3.5-9b", "nomic-embed-text"])):
        c = ob.check_ollama("http://localhost:11434")
    assert c["status"] == "ok"
    assert "qwen3.5-9b" in c["detail"]


def test_check_ollama_down_is_missing_with_fix():
    with mock.patch.object(urllib.request, "urlopen", side_effect=ConnectionError("refused")):
        c = ob.check_ollama("http://localhost:11434")
    assert c["status"] == "missing"
    assert "ollama.com" in c["fix"]


# --- Hardware (sin admin, sin GPU) ----------------------------------------

def test_detect_hardware_no_gpu_marks_unknown_or_none():
    fake_mem = mock.Mock(total=8 * 1024 ** 3)
    fake_psutil = mock.Mock(virtual_memory=mock.Mock(return_value=fake_mem))
    with (
        mock.patch.dict("sys.modules", {"psutil": fake_psutil}),
        mock.patch.object(ob._shutil, "which", return_value=None),
        mock.patch.object(ob._shutil, "disk_usage", return_value=(100, 60, 40 * 1024 ** 3)),
    ):
        hw = ob.detect_hardware()
    assert hw["ram_gb"] == 8.0
    assert hw["vram_gb"] == "unknown"
    assert hw["disk_free_gb"] == 40.0


def test_check_docker_absent_is_manual_pending():
    with mock.patch.object(ob._shutil, "which", return_value=None):
        c = ob.check_docker()
    assert c["status"] == "manual-pending"
    assert "winget" in c["fix"]


def test_check_docker_no_daemon_is_error_not_manual():
    with (
        mock.patch.object(ob._shutil, "which", return_value="docker"),
        mock.patch.object(
            ob._subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["docker", "info"], 1, "", "daemon not running"),
        ),
    ):
        c = ob.check_docker()
    assert c["status"] == "error"
    assert "Docker Desktop" in c["fix"]


# --- Politica por perfiles -------------------------------------------------

def test_plan_models_light_profile():
    hw = {"gpu_name": "none-detected", "ram_gb": 6.0, "vram_gb": "unknown"}
    pending = ob.plan_models(hw, ["nomic-embed-text"])
    names = [p["name"] for p in pending]
    assert names == ["qwen3.5-9b"]
    assert pending[0]["pull_cmd"] == "ollama pull qwen3.5-9b"


def test_plan_models_workstation_adds_coder_and_vl():
    hw = {"gpu_name": "nvidia", "ram_gb": 32.0, "vram_gb": 16.0}
    pending = ob.plan_models(hw, [])
    names = [p["name"] for p in pending]
    assert names == ["qwen3.5-9b", "nomic-embed-text", "qwen2.5-coder:7b", "qwen3-vl-8b"]
    assert all(p["pull_cmd"].startswith("ollama pull ") for p in pending)


def test_plan_models_cpu_only_uses_nemotron():
    hw = {"gpu_name": "none-detected", "ram_gb": 8.0, "vram_gb": "unknown"}
    pending = ob.plan_models(hw, [])
    names = [p["name"] for p in pending]
    assert "nemotron-3-nano:4b" in names
    assert "qwen2.5-coder:7b" not in names


def test_plan_models_nothing_pending_when_complete():
    hw = {"gpu_name": "nvidia", "ram_gb": 32.0, "vram_gb": 16.0}
    full = ["qwen3.5-9b", "nomic-embed-text", "qwen2.5-coder:7b", "qwen3-vl-8b"]
    assert ob.plan_models(hw, full) == []


# --- Pulls simulados -------------------------------------------------------

def test_ensure_models_runs_exact_pull_cmds():
    seen = []

    def _fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "pulled", "")

    pending = [
        {"name": "nomic-embed-text", "pull_cmd": "ollama pull nomic-embed-text", "reason": "x"},
        {"name": "qwen3.5-9b", "pull_cmd": "ollama pull qwen3.5-9b", "reason": "y"},
    ]
    logs: list[str] = []
    with (
        mock.patch.object(ob._shutil, "which", return_value="ollama"),
        mock.patch.object(ob._subprocess, "run", side_effect=_fake_run),
    ):
        results = ob.ensure_ollama_models(pending, log=logs.append)
    assert seen == [["ollama", "pull", "nomic-embed-text"], ["ollama", "pull", "qwen3.5-9b"]]
    assert all(r["status"] == "ok" for r in results)
    assert any("qwen3.5-9b" in m for m in logs)


def test_ensure_models_without_binary_errors_with_fix():
    pending = [{"name": "nomic-embed-text", "pull_cmd": "ollama pull nomic-embed-text", "reason": "x"}]
    with mock.patch.object(ob._shutil, "which", return_value=None):
        results = ob.ensure_ollama_models(pending)
    assert results[0]["status"] == "error"
    assert "ollama.com" in results[0]["fix"]


def test_write_docker_guide_has_winget(tmp_path):
    guide = ob.write_docker_guide(tmp_path)
    text = Path(guide).read_text(encoding="utf-8")
    assert ob.DOCKER_WINGET_CMD in text
    assert "WSL2" in text


def test_run_doctor_shape_with_mocks():
    with (
        mock.patch.object(ob, "check_ollama", return_value=ob._make_check("ollama", "ok", "modelo(s): ninguno", "")),
        mock.patch.object(ob, "check_docker", return_value=ob._make_check("docker", "manual-pending", "falta", "guia")),
        mock.patch.object(ob, "check_network", return_value=ob._make_check("red", "unknown", "?", "")),
        mock.patch.object(ob, "detect_hardware", return_value={"gpu_name": "none-detected", "ram_gb": 6.0, "vram_gb": "unknown"}),
    ):
        report = ob.run_doctor()
    assert set(report) >= {"ok", "checks", "profile", "hardware", "ollama_models"}
    assert report["ok"] is False  # docker manual-pending bloquea el OK
    assert all(set(c) == {"name", "status", "detail", "fix"} for c in report["checks"])
    assert "qwen3.5-9b" in [p["name"] for p in ob.plan_models(report["hardware"], report["ollama_models"])]


def test_run_setup_check_only_does_not_pull(capsys):
    with (
        mock.patch.object(ob, "run_doctor", return_value={
            "ok": False, "profile": "light",
            "hardware": {"gpu_name": "none-detected", "ram_gb": 6.0, "vram_gb": "unknown"},
            "ollama_models": [],
            "checks": [ob._make_check("ollama", "ok", "modelo(s): ninguno", "")],
        }),
        mock.patch.object(ob, "ensure_ollama_models", side_effect=AssertionError("no debe instalar en --check")),
    ):
        rc = ob.run_setup(fix=False)
    assert rc == 1
    out = capsys.readouterr().out
    assert "ollama pull qwen3.5-9b" in out
