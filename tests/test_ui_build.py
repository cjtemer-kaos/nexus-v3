"""Sección [7] UI build (paridad run_tests.py)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"

pytestmark = pytest.mark.ui

_HAS_NPX = shutil.which("npx") is not None
_HAS_NPM = shutil.which("npm") is not None
_NODE = shutil.which("node")
_TSC_JS = UI_DIR / "node_modules" / "typescript" / "bin" / "tsc"


def test_package_json_exists():
    assert (UI_DIR / "package.json").exists()


@pytest.mark.skipif(
    not (_HAS_NPX or (_NODE and _TSC_JS.exists())),
    reason="npx/node+typescript no disponibles en esta PC",
)
def test_tsc_noemit():
    if _NODE and _TSC_JS.exists():
        cmd = [_NODE, str(_TSC_JS), "--noEmit"]
        use_shell = False
    else:
        cmd = "npx --no-install tsc --noEmit"
        use_shell = True
    r = subprocess.run(
        cmd,
        cwd=str(UI_DIR),
        capture_output=True,
        text=True,
        timeout=240,
        shell=use_shell,
    )
    assert r.returncode == 0, f"returncode={r.returncode} stderr={r.stderr[:500]} stdout={r.stdout[:500]}"


@pytest.mark.skipif(not (_HAS_NPM and (UI_DIR / "node_modules").exists()), reason="npm/node_modules no disponibles en esta PC")
def test_vite_build():
    r2 = subprocess.run(
        "npm run build",
        cwd=str(UI_DIR),
        capture_output=True,
        text=True,
        timeout=300,
        shell=True,
    )
    assert r2.returncode == 0, f"returncode={r2.returncode} stdout={r2.stdout[-800:]} stderr={r2.stderr[-800:]}"


@pytest.mark.skipif(not (UI_DIR / "dist" / "index.html").exists(), reason="ui/dist no construido (gitignored) — correr npm run build")
def test_dist_artifacts():
    dist = UI_DIR / "dist"
    assert (dist / "index.html").exists()
    assets = list((dist / "assets").glob("*")) if (dist / "assets").exists() else []
    assert len(assets) >= 1


def test_vite_config_exists():
    assert (UI_DIR / "vite.config.ts").exists()


def test_app_tsx_structure():
    app_text = (UI_DIR / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "Sidebar" in app_text and "ChatPanel" in app_text
