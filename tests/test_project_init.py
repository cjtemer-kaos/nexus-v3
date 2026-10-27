"""Generador de AGENTS.md (`/init`).

Lo que se fija: que el borrador salga de datos reales del disco, que no pise un
AGENTS.md existente sin pedirlo, y que un repo desconocido igual produzca algo
válido en vez de reventar.
"""
from __future__ import annotations

import json

from src.core import project_init as pi


def test_detecta_python_con_tests(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    info = pi.analyze(tmp_path)
    assert "Python" in info.stacks
    assert info.manifest == "pyproject.toml"
    assert info.test_command == "python -m pytest -q"


def test_detecta_node_leyendo_scripts(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest", "build": "vite build", "dev": "vite"}}),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    info = pi.analyze(tmp_path)
    assert "Node.js" in info.stacks
    assert info.package_manager == "pnpm"
    assert info.test_command == "pnpm run test"
    assert info.build_command == "pnpm run build"
    assert info.run_command == "pnpm run dev"


def test_node_sin_script_test_no_inventa_comando(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8")
    info = pi.analyze(tmp_path)
    assert info.test_command == ""
    assert info.build_command == "npm run build"


def test_package_json_roto_no_rompe(tmp_path):
    (tmp_path / "package.json").write_text("{ esto no es json", encoding="utf-8")
    info = pi.analyze(tmp_path)
    assert "Node.js" in info.stacks
    assert info.test_command == ""


def test_bom_utf8_no_deja_el_repo_sin_comandos(tmp_path):
    """Un package.json guardado en Windows puede traer BOM: json.loads falla y el
    borrador decía "no se detectó un runner" con los scripts a la vista."""
    (tmp_path / "package.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}).encode("utf-8")
    )
    info = pi.analyze(tmp_path)
    assert "Node.js" in info.stacks
    assert info.test_command == "npm run test"
    assert info.build_command == "npm run build"


def test_detecta_rust_y_go(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    info = pi.analyze(tmp_path)
    assert info.test_command == "cargo test"

    otro = tmp_path / "go"
    otro.mkdir()
    (otro / "go.mod").write_text("module x\n", encoding="utf-8")
    assert pi.analyze(otro).test_command == "go test ./..."


def test_ignora_directorios_de_ruido(tmp_path):
    for d in ("src", "node_modules", ".venv", "dist", "tests", "_attic"):
        (tmp_path / d).mkdir()
    info = pi.analyze(tmp_path)
    assert "src/" in info.dirs
    assert "tests/" in info.dirs
    assert "node_modules/" not in info.dirs
    assert "dist/" not in info.dirs
    assert "_attic/" not in info.dirs


def test_no_pisa_agents_md_existente(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# mío\n", encoding="utf-8")
    out = pi.write_agents_md(tmp_path)
    assert out["created"] is False
    assert "ya existe" in str(out["reason"])
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == "# mío\n"


def test_force_reemplaza_y_avisa(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# viejo\n", encoding="utf-8")
    out = pi.write_agents_md(tmp_path, force=True)
    assert out["created"] is True
    assert out["replaced"] is True
    assert "viejo" not in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_repo_desconocido_igual_genera_algo_valido(tmp_path):
    out = pi.write_agents_md(tmp_path)
    assert out["created"] is True
    texto = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "sin detectar" in texto
    assert "## Comandos" in texto
    assert "## Reglas" in texto


def test_el_borrador_incluye_lo_detectado(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "main.py").write_text("print(1)\n", encoding="utf-8")
    texto = pi.render(pi.analyze(tmp_path))
    assert "python -m pytest -q" in texto
    assert "`main.py`" in texto
    assert "`tests/`" in texto


def test_no_lee_secretos(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=secreto-que-no-debe-salir\n", encoding="utf-8")
    out = pi.write_agents_md(tmp_path)
    texto = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert out["created"] is True
    assert "secreto-que-no-debe-salir" not in texto
