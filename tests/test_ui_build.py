"""Sección [7] UI build (paridad run_tests.py)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_DIR = ROOT / "ui"

pytestmark = pytest.mark.ui


def test_package_json_exists():
    assert (UI_DIR / "package.json").exists()


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
