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


def test_package_json_exists():
    assert (UI_DIR / "package.json").exists()


@pytest.mark.skipif(not _HAS_NPX, reason="npx no disponible en esta PC")
def test_tsc_noemit():
    r = subprocess.run(
        "npx tsc --noEmit",
        cwd=str(UI_DIR),
        capture_output=True,
        text=True,
        timeout=60,
        shell=True,
    )
    assert r.returncode == 0, f"returncode={r.returncode} stderr={r.stderr[:500]} stdout={r.stdout[:500]}"


@pytest.mark.skipif(not (_HAS_NPM and (UI_DIR / "node_modules").exists()), reason="npm/node_modules no disponibles en esta PC")
def test_vite_build():
    r2 = subprocess.run(
        "npm run build",
        cwd=str(UI_DIR),
        capture_output=True,
        text=True,
        timeout=90,
        shell=True,
    )
    assert r2.returncode == 0, f"returncode={r2.returncode} stdout={r2.stdout[-800:]} stderr={r2.stderr[-800:]}"


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
