"""skill(query=...): avisar cuando NO hay una skill para el tema.

Con 2.161 skills instaladas siempre hay *algun* match por coincidencias sueltas
del cuerpo: `skill(query="zzzz-no-existe-xyz")` devolvia 6 resultados con score
0.8-1.2 presentados igual que una busqueda buena. Un modelo chico carga la
primera y sigue pasos de una skill que no tiene nada que ver.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import server as S  # noqa: E402


@pytest.fixture(scope="module")
def buscar():
    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, f"test-skill-{uuid.uuid4().hex[:8]}", cfg)

    def usar(query: str, limit: int = 10):
        raw = asyncio.run(reg.dispatch("skill", {"query": query, "limit": limit},
                                       scope=scope, hooks=None))
        return json.loads(raw)

    return usar


def test_busqueda_sin_tema_avisa_que_no_hay_skill(buscar):
    out = buscar("zzzz-no-existe-xyz")

    assert "aviso" in out, out
    assert "probablemente NO hay" in out["aviso"]
    assert all(float(r["score"]) < S.UMBRAL_SKILL_FLOJO for r in out["results"])


def test_busqueda_buena_no_avisa(buscar):
    out = buscar("python-testing")

    assert out["count"] > 0
    assert "aviso" not in out, out
    assert float(out["results"][0]["score"]) >= S.UMBRAL_SKILL_FLOJO


def test_busqueda_de_una_palabra_del_tema_encuentra_el_nombre(buscar):
    out = buscar("blender")

    nombres = [r["name"] for r in out["results"]]
    assert any("blender" in n.lower() for n in nombres), nombres
    assert "aviso" not in out


def test_umbral_esta_por_encima_del_ruido_medido():
    """Ruido real medido: 0.81-1.2. Consultas legitimas: >= 11."""
    assert 3.0 <= S.UMBRAL_SKILL_FLOJO <= 8.0
