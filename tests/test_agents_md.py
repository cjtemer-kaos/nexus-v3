"""AGENTS.md jerárquico: qué se inyecta y qué pasa cuando no entra.

El defecto que fijan estos tests: `system_block` cortaba con `break` al primer
archivo que no entraba en el presupuesto. Como el del root se lee primero, en
cuanto crecía el del subdirectorio **desaparecía sin aviso** y el agente perdía
las reglas de la carpeta que iba a tocar (pasó de verdad: una regla nueva en
`src/core/AGENTS.md` dejó ese archivo afuera del prompt y rompió el E2E).
"""
from __future__ import annotations

from src.core.agents_md import find_agents_md, system_block


def _proyecto(tmp_path, root_md: str, core_md: str):
    (tmp_path / "AGENTS.md").write_text(root_md, encoding="utf-8")
    core = tmp_path / "src" / "core"
    core.mkdir(parents=True)
    (core / "AGENTS.md").write_text(core_md, encoding="utf-8")
    return core


def test_incluye_el_root_y_el_del_subdirectorio(tmp_path):
    core = _proyecto(tmp_path, "# root\nreglas generales", "# core\nreglas del nucleo")

    rels = [r for r, _ in find_agents_md([str(core / "x.py")], tmp_path)]
    assert rels == ["AGENTS.md", "src/core/AGENTS.md"]

    bloque = system_block([str(core / "x.py")], tmp_path)
    assert "reglas generales" in bloque
    assert "reglas del nucleo" in bloque
    assert "src/core/AGENTS.md" in bloque


def test_presupuesto_chico_recorta_pero_no_descarta(tmp_path):
    core = _proyecto(tmp_path, "# root\n" + "r" * 4000, "# core\n" + "c" * 3000)

    # El root entra; el del subdirectorio ya no. Antes desaparecía del prompt.
    bloque = system_block([str(core / "x.py")], tmp_path, max_chars=5000)

    assert "src/core/AGENTS.md" in bloque
    assert "(recortado)" in bloque
    assert len(bloque) <= 5000


def test_sin_agents_md_devuelve_vacio(tmp_path):
    assert find_agents_md([], tmp_path) == []
    assert system_block([], tmp_path) == ""
