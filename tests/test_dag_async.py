"""Regresion: la descomposicion de objetivos no puede romper el event loop.

`DAGCoordinator.decompose_goal` era sincronica y, para objetivos complejos,
hacia `asyncio.get_event_loop().run_until_complete(...)`. Cuando la llamaba el
bridge MCP (`dag_decompose`, que es `async def`), ya habia un loop corriendo y
la llamada terminaba en `RuntimeError: This event loop is already running`
justo para los objetivos que necesitan el LLM. El bridge atrapaba la excepcion
y devolvia `{"error": ...}`, asi que la feature parecia "no disponible".

Ademas el bridge reportaba `method: "llm"` siempre que `use_llm=True`, aunque
`_decompose_with_llm` hubiera caido a patrones porque Ollama no respondio.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.core.dag_coordinator import DAGCoordinator, TaskNode

OBJETIVO_COMPLEJO = (
    "primero investigar el estado del arte de los harness de agentes y las "
    "alternativas open source, luego disenar la arquitectura del modulo de "
    "memoria con jerarquia de tres niveles y finalmente implementar las pruebas"
)


def _nodos_falsos(_self, goal: str):
    async def _coro():
        return [TaskNode(id="t01", title="una", description=goal, assignee="code")], "llm"
    return _coro()


@pytest.fixture()
def llm_falso(monkeypatch):
    monkeypatch.setattr(DAGCoordinator, "_decompose_with_llm", _nodos_falsos)


def test_el_objetivo_de_prueba_es_complejo():
    """Si esto deja de ser complejo, los tests de abajo no prueban el bug."""
    assert DAGCoordinator()._needs_llm(OBJETIVO_COMPLEJO) is True


def test_decompose_async_no_explota_dentro_de_un_loop(llm_falso):
    dc = DAGCoordinator()

    async def escenario():
        return await dc.decompose_goal_async(OBJETIVO_COMPLEJO, use_llm=True)

    dag = asyncio.run(escenario())
    assert dag.method == "llm"
    assert [n.id for n in dag.nodes] == ["t01"]


def test_decompose_sincronico_desde_un_loop_falla_cerrado(llm_falso):
    """Sin loop no hay drama; con loop tiene que avisar, no degradar mudo."""
    dc = DAGCoordinator()

    async def escenario():
        with pytest.raises(RuntimeError) as exc:
            dc.decompose_goal(OBJETIVO_COMPLEJO, use_llm=True)
        return str(exc.value)

    msg = asyncio.run(escenario())
    assert "decompose_goal_async" in msg


def test_decompose_sincronico_sin_loop_sigue_andando(llm_falso):
    """Compat: el camino sync (tests, scripts) no puede romperse."""
    dag = DAGCoordinator().decompose_goal(OBJETIVO_COMPLEJO, use_llm=True)
    assert dag.method == "llm"
    assert len(dag.nodes) == 1


def test_el_metodo_reporta_patron_si_el_llm_falla(monkeypatch):
    """Ollama caido -> `_decompose_with_llm` devuelve el patron, y el DAG lo dice."""
    async def _falla_a_patron(self, goal):
        return self._parse_goal(goal), "pattern"

    monkeypatch.setattr(DAGCoordinator, "_decompose_with_llm", _falla_a_patron)
    dag = asyncio.run(DAGCoordinator().decompose_goal_async(OBJETIVO_COMPLEJO, use_llm=True))
    assert dag.method == "pattern"


def test_el_bridge_expone_el_metodo_real(llm_falso):
    """El bug original: `dag_decompose` devolvia error de event loop."""
    from src.bridges import mcp_bridge_server as bridge

    out = json.loads(asyncio.run(bridge.dag_decompose(OBJETIVO_COMPLEJO, use_llm=True)))
    assert "error" not in out, out
    assert out["method"] == "llm"
    assert out["task_count"] == 1


def test_el_bridge_sin_llm_reporta_patron():
    from src.bridges import mcp_bridge_server as bridge

    out = json.loads(asyncio.run(bridge.dag_decompose("crear una api", use_llm=False)))
    assert "error" not in out, out
    assert out["method"] == "pattern"
    assert out["task_count"] >= 1
