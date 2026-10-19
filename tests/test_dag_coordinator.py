"""Tests del coordinador DAG: descomposicion por patrones y ejecucion por niveles."""
import asyncio

from src.core.dag_coordinator import (
    DAGCoordinator,
    TaskDAG,
    TaskNode,
    TaskStatus,
)


def test_descompone_objetivo_de_construccion():
    dag = DAGCoordinator().decompose_goal("crear un servicio web", use_llm=False)
    ids = [n.id for n in dag.nodes]
    assert ids == ["t01_plan", "t02_design", "t03_implement", "t04_test", "t05_review"]
    assert dag.nodes[0].depends_on == []
    assert dag.nodes[1].depends_on == ["t01_plan"]
    assert dag.nodes[4].depends_on == ["t04_test"]
    assert dag.nodes[2].assignee == "code"


def test_descompone_busqueda_y_debug():
    research = DAGCoordinator().decompose_goal("investigar el mercado", use_llm=False)
    assert [n.id for n in research.nodes] == ["t01_scope", "t02_collect", "t03_analyze", "t04_report"]

    debug = DAGCoordinator().decompose_goal("arreglar el bug de login", use_llm=False)
    assert [n.id for n in debug.nodes] == ["t01_reproduce", "t02_diagnose", "t03_fix", "t04_verify"]
    assert debug.nodes[2].assignee == "code"


def test_objetivo_simple_es_una_sola_tarea():
    dag = DAGCoordinator().decompose_goal("resumir el documento", use_llm=False)
    assert len(dag.nodes) == 1
    assert dag.nodes[0].description == "resumir el documento"


def test_objetivo_largo_sin_patron_se_parte_en_dos():
    goal = " ".join(["palabra"] * 14)
    dag = DAGCoordinator().decompose_goal(goal, use_llm=False)
    assert [n.id for n in dag.nodes] == ["t01_understand", "t02_execute"]
    assert dag.nodes[1].depends_on == ["t01_understand"]


def test_ready_tasks_respeta_dependencias():
    dag = DAGCoordinator().decompose_goal("crear una api", use_llm=False)
    assert [n.id for n in dag.get_ready_tasks()] == ["t01_plan"]
    dag.nodes[0].status = TaskStatus.COMPLETED
    assert [n.id for n in dag.get_ready_tasks()] == ["t02_design"]
    assert dag.get_completion_percent() == 20.0
    assert dag.is_complete() is False


def test_dag_vacio_esta_completo():
    dag = TaskDAG(id="vacio", goal="nada")
    assert dag.get_completion_percent() == 100.0
    assert dag.is_complete() is True


def test_execute_dag_corre_por_niveles_y_respeta_orden():
    coord = DAGCoordinator()
    dag = coord.decompose_goal("crear algo", use_llm=False)
    orden = []

    async def executor(description, assignee):
        orden.append(assignee)
        return {"content": f"ok:{assignee}"}

    summary = asyncio.run(coord.execute_dag(dag, executor_func=executor))

    assert summary["status"] == "completed"
    assert summary["completed"] == 5
    assert summary["failed"] == 0
    assert summary["completion_percent"] == 100.0
    assert orden[0] == "architect"
    assert orden.index("code") < orden.index("tester") < orden.index("optimizer")
    assert dag.nodes[3].result == "ok:tester"
    assert coord.get_run(dag.id) is dag
    assert coord.list_runs()[0]["id"] == dag.id


def test_execute_dag_sin_executor_marca_completadas():
    coord = DAGCoordinator()
    dag = coord.decompose_goal("resumir esto", use_llm=False)
    summary = asyncio.run(coord.execute_dag(dag))
    assert summary["completed"] == 1
    assert dag.nodes[0].result == "Executed (no executor)"


def test_execute_dag_fallido_queda_parcial():
    coord = DAGCoordinator()
    dag = coord.decompose_goal("arreglar el bug", use_llm=False)
    for node in dag.nodes:
        node.max_retries = 0

    async def executor(description, assignee):
        raise ValueError("boom")

    summary = asyncio.run(coord.execute_dag(dag, executor_func=executor))

    assert summary["status"] == "partial"
    assert summary["failed"] == 1
    assert summary["skipped"] == 3
    assert dag.nodes[0].status == TaskStatus.FAILED
    assert all(n.status == TaskStatus.SKIPPED for n in dag.nodes[1:])
    assert "boom" in dag.nodes[0].error


def test_nodos_hijos_no_corren_si_el_padre_falla():
    coord = DAGCoordinator()
    dag = TaskDAG(id="x", goal="dos pasos", nodes=[
        TaskNode(id="a", title="A", description="a", max_retries=0),
        TaskNode(id="b", title="B", description="b", depends_on=["a"], max_retries=0),
    ])
    ejecutados = []

    async def executor(description, assignee):
        ejecutados.append(description)
        if description == "a":
            raise RuntimeError("falla el primero")
        return {"content": "ok"}

    summary = asyncio.run(coord.execute_dag(dag, executor_func=executor))

    assert ejecutados == ["a"]
    assert summary["failed"] == 1
    assert summary["completed"] == 0
    assert summary["skipped"] == 1
    assert dag.nodes[1].status == TaskStatus.SKIPPED
    assert "dependencia fallida" in dag.nodes[1].error
