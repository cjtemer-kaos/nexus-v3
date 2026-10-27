"""scripts/skills_import.py: varias fuentes en una sola corrida.

`--from` era un unico string: para dejar nexus con las mismas skills que ve otro
agente habia que correr el script una vez por hub (y la segunda pasada pisaba a
la primera sin decir nada). Ahora `--from` es repetible y `--from-opencode`
agrega las carpetas donde opencode/Claude Code instalan skills.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import skills_import as si  # noqa: E402


def _skill(base: Path, nombre: str, desc: str) -> Path:
    md = base / nombre / "SKILL.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(f"---\nname: {nombre}\ndescription: {desc}\n---\n\nCuerpo.\n", encoding="utf-8")
    return md


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    raiz = tmp_path / "proyecto"
    raiz.mkdir()
    destino = raiz / "data" / "skills"
    monkeypatch.setattr(si, "RAIZ", raiz)
    monkeypatch.setattr(si, "DESTINO", destino)
    return raiz, destino


def test_dos_fuentes_en_una_corrida(sandbox, tmp_path, capsys):
    raiz, destino = sandbox
    a, b = tmp_path / "hubA", tmp_path / "hubB"
    _skill(a, "uno", "de A")
    _skill(b, "dos", "de B")

    rc = si.main(["--from", str(a), "--from", str(b), "--to", str(destino)])

    assert rc == 0
    assert (destino / "uno" / "SKILL.md").is_file()
    assert (destino / "dos" / "SKILL.md").is_file()
    assert "Skills en destino: 2" in capsys.readouterr().out


def test_la_ultima_fuente_gana_en_ruta_repetida(sandbox, tmp_path):
    raiz, destino = sandbox
    a, b = tmp_path / "hub", tmp_path / "personal"
    _skill(a, "igual", "version del hub")
    _skill(b, "igual", "version personal")

    assert si.main(["--from", str(a), "--from", str(b), "--to", str(destino)]) == 0
    texto = (destino / "igual" / "SKILL.md").read_text(encoding="utf-8")

    assert "version personal" in texto


def test_from_opencode_agrega_las_carpetas_estandar(sandbox, monkeypatch, tmp_path):
    raiz, destino = sandbox
    home = tmp_path / "home"
    _skill(home / ".claude" / "skills", "ecc-uno", "del bundle")
    _skill(home / ".config" / "opencode" / "skills", "mi-skill", "personal")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert si.main(["--from-opencode", "--to", str(destino)]) == 0
    assert (destino / "ecc-uno" / "SKILL.md").is_file()
    assert (destino / "mi-skill" / "SKILL.md").is_file()


def test_sin_fuentes_es_error_de_uso(sandbox, monkeypatch, capsys):
    monkeypatch.delenv("NEXUS_V3_SKILLS_SRC", raising=False)
    raiz, destino = sandbox

    assert si.main(["--to", str(destino)]) == 2
    assert "falta --from" in capsys.readouterr().out


def test_origen_inexistente_no_copia_nada(sandbox, tmp_path):
    raiz, destino = sandbox
    bueno = tmp_path / "hub"
    _skill(bueno, "uno", "ok")

    rc = si.main(["--from", str(bueno), "--from", str(tmp_path / "no-existe"),
                  "--to", str(destino)])

    assert rc == 2
    assert not destino.exists()


def test_destino_fuera_del_proyecto_se_rechaza(sandbox, tmp_path):
    raiz, _ = sandbox
    hub = tmp_path / "hub"
    _skill(hub, "uno", "ok")

    assert si.main(["--from", str(hub), "--to", str(tmp_path / "afuera")]) == 2
