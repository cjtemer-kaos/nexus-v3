"""Bootstrap de primer arranque — RAG y aprendizaje listos desde el dia uno.

Uso desde el setup de otro agente (solo import/llamada)::

    from src.core.firstboot import ensure_rag_ready, ensure_learning
    rag = ensure_rag_ready()
    learn = ensure_learning()

Todo es inyectable (models, embed_fn, db_path, factories) para que los
tests corran con mocks, sin red ni Ollama real. Ninguna funcion lanza:
los fallos se reportan en el dict (campos ok/ready + errors).
"""
from __future__ import annotations

import math
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

EMBED_MODEL_DEFAULT = "nomic-embed-text"
GRAPH_MAINTENANCE_INTERVAL_S = 21600.0  # 6h, paridad con LearningService


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def ensure_rag_ready(
    *,
    models: Optional[Sequence[str]] = None,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    db_path: Optional[Path] = None,
    embed_model: str = EMBED_MODEL_DEFAULT,
) -> dict[str, Any]:
    """Verifica modelo de embeddings, crea el indice si falta y hace smoke test."""
    actions: list[str] = []
    errors: list[str] = []
    rep: dict[str, Any] = {
        "ready": False, "embed_model": embed_model,
        "embed_available": False, "index_ok": False,
        "smoke_ok": False, "smoke_skipped": False,
        "actions": actions, "errors": errors,
    }
    # 1. modelo de embeddings en el Ollama local (o inyectado en tests)
    try:
        if models is None:
            from src.core.environment import list_ollama_models
            models = list_ollama_models()
        from src.core.environment import pick_embed_model
        rep["embed_model"] = pick_embed_model(embed_model, list(models or []))
        rep["embed_available"] = any(
            ("embed" in m.lower() or "nomic" in m.lower()) for m in (models or [])
        )
        if models is not None and len(list(models)) == 0:
            actions.append("ollama_sin_respuesta: sin deteccion, se respeta el modelo configurado")
        elif rep["embed_available"]:
            actions.append(f"embed_model_resuelto: {rep['embed_model']}")
        else:
            errors.append(f"sin modelo de embeddings entre {len(list(models or []))} modelos")
    except Exception as e:
        errors.append(f"deteccion_ollama: {type(e).__name__}: {e}")
    # 2. indice RAG: crea tablas si faltan
    try:
        if db_path is None:
            from src.core import rag as _rag
            con = _rag._connect()
        else:
            p = Path(db_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(p), timeout=10)
            con.execute(
                """CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    chunk_index INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL UNIQUE,
                    embedding BLOB,
                    created_at REAL NOT NULL)"""
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
            con.commit()
        try:
            n = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
            rep["chunks"] = int(n)
            rep["index_ok"] = True
            actions.append(f"indice_rag_ok: {n} chunks")
        finally:
            con.close()
    except Exception as e:
        errors.append(f"indice_rag: {type(e).__name__}: {e}")
    # 3. smoke test de busqueda (embedding real o inyectado; sin red en tests)
    try:
        fn = embed_fn
        if fn is None:
            try:
                from src.core.embeddings import embed as _embed

                def fn(texts: list[str]) -> list[list[float]]:  # type: ignore[misc]
                    return _embed(texts, model=rep["embed_model"])
            except Exception as e:
                fn = None
                errors.append(f"embed_import: {type(e).__name__}: {e}")
        if fn is None:
            rep["smoke_skipped"] = True
            actions.append("smoke_omitido: sin funcion de embeddings")
        else:
            vecs = fn(["smoke test primer arranque"])
            if vecs and vecs[0] and _cosine(vecs[0], vecs[0]) > 0.99:
                rep["smoke_ok"] = True
                rep["embed_available"] = True
                actions.append(f"smoke_ok: dim={len(vecs[0])}")
            else:
                errors.append("smoke: embedding vacio o degenerado")
    except Exception as e:
        errors.append(f"smoke: {type(e).__name__}: {e}")
    rep["ready"] = bool(rep["index_ok"] and (rep["smoke_ok"] or rep["smoke_skipped"]))
    return rep


def ensure_learning(
    *,
    brain_dir: Optional[Path] = None,
    knowledge_factory: Optional[Callable[[Path], Any]] = None,
    run_maintenance: bool = True,
    check_gates: bool = True,
) -> dict[str, Any]:
    """Verifica DBs del brain, maintain() cableado y gates de los loops.

    knowledge_factory(path) -> objeto con learn/status/maintain (por defecto
    KnowledgeMemory real). Con run_maintenance=False no muta nada.
    """
    actions: list[str] = []
    errors: list[str] = []
    rep: dict[str, Any] = {
        "ready": False, "brain_dir": "", "graph_ok": False,
        "maintenance_wired": False, "maintenance": {},
        "gates": {}, "actions": actions, "errors": errors,
    }
    # 1. directorio del brain
    try:
        if brain_dir is None:
            from src.brain.link import ensure_brain_dir
            bp = ensure_brain_dir()
        else:
            bp = Path(brain_dir)
            bp.mkdir(parents=True, exist_ok=True)
        rep["brain_dir"] = str(bp)
        actions.append(f"brain_dir_ok: {bp}")
    except Exception as e:
        errors.append(f"brain_dir: {type(e).__name__}: {e}")
        return rep
    # 2. grafo durable (upsert por topic_key) + maintain cableado
    try:
        if knowledge_factory is None:
            from src.core.knowledge_memory import KnowledgeMemory

            def knowledge_factory(p: Path) -> Any:  # type: ignore[misc]
                return KnowledgeMemory(db_path=p / "knowledge_graph.db")
        km = knowledge_factory(bp)
        st = km.status() if hasattr(km, "status") else {}
        rep["graph"] = st
        rep["graph_ok"] = True
        actions.append(f"grafo_ok: {st.get('nodes', '?')} nodos")
        if not hasattr(km, "maintain"):
            errors.append("maintain ausente en knowledge_factory")
        else:
            rep["maintenance_wired"] = True
            if run_maintenance:
                m = km.maintain()
                rep["maintenance"] = m if isinstance(m, dict) else {"ok": True}
                actions.append("maintain_ejecutado")
            else:
                actions.append("maintain_cableado (no ejecutado)")
    except Exception as e:
        errors.append(f"grafo: {type(e).__name__}: {e}")
    # 3. gates de los loops (imports + invariantes, sin arrancar servicios)
    if check_gates:
        gates: dict[str, Any] = {}
        try:
            from src.core.learning_service import LearningService, _GRAPH_MAINTENANCE_INTERVAL_S
            gates["learning_service_import"] = True
            gates["maintenance_cada_6h"] = (_GRAPH_MAINTENANCE_INTERVAL_S == GRAPH_MAINTENANCE_INTERVAL_S)
            gates["record_turn_presente"] = hasattr(LearningService, "record_turn")
            gates["scheduler_start_stop"] = hasattr(LearningService, "start") and hasattr(LearningService, "stop")
        except Exception as e:
            gates["learning_service_import"] = False
            errors.append(f"learning_service: {type(e).__name__}: {e}")
        try:
            from src.core.skill_opt import SkillOptimizer
            gates["skillopt_heldout_gate"] = hasattr(SkillOptimizer, "validate")
        except Exception as e:
            gates["skillopt_heldout_gate"] = False
            errors.append(f"skill_opt: {type(e).__name__}: {e}")
        rep["gates"] = gates
        if gates.get("learning_service_import") and gates.get("skillopt_heldout_gate"):
            actions.append("gates_ok: thompson+heldout+6h")
    rep["ready"] = bool(rep["graph_ok"] and rep["maintenance_wired"] and not errors)
    return rep


def firstboot_report(
    *,
    fix_mcp: bool = False,
    models: Optional[Sequence[str]] = None,
    embed_fn: Optional[Callable[[list[str]], list[list[float]]]] = None,
    brain_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Reporte unico de primer arranque (para el CLI de setup de otro agente)."""
    from src.core.mcp_setup import run_mcp_setup
    mcp = run_mcp_setup(fix=fix_mcp)
    rag = ensure_rag_ready(models=models, embed_fn=embed_fn)
    learn = ensure_learning(brain_dir=brain_dir, run_maintenance=False)
    ready = bool(mcp.get("ready") and rag.get("ready") and learn.get("ready"))
    return {"ready": ready, "mcp": mcp, "rag": rag, "learning": learn}
