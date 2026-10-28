"""Memoria proactiva y presupuesto real de contexto.

Tres fallos reales que estos tests fijan:

1. El agente arrancaba sin mirar su grafo y concluia "rea no esta instalado"
   aunque el grafo tenia el nodo que explicaba como usarlo.
2. La busqueda de esa memoria devolvia los episodios que el harness crea por
   sesion (repiten el pedido entero) y tapaba el conocimiento curado.
3. El presupuesto de contexto no contaba los JSON Schema de las tools (~7.3k
   tokens), asi que compactaba a los 6.6k de 24k y el resumen perdia la lista
   de tools recien pedida.
"""
from __future__ import annotations

import asyncio

import pytest

from src.core.agent_loop import AgentLoop
from src.core.compaction import CompactionPipeline
from src.core.knowledge_memory import (
    KnowledgeMemory,
    fts_search_tolerant,
    get_knowledge_memory,
    query_terms,
    reset_knowledge_memory,
)

PEDIDO = ("quiero que uses rea para analizarlo y hacer una version mejorada, "
          "ese programa ya es viejo y lento, pero la red p2p sigue viva")


@pytest.fixture()
def km(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.hybrid_memory import HybridMemoryBackend
    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    reset_knowledge_memory()
    k = KnowledgeMemory(db_path=tmp_path / "knowledge_graph.db", rag_index=False)
    yield k
    reset_knowledge_memory()


def test_query_terms_conserva_lo_que_discrimina():
    terms = query_terms(PEDIDO)
    assert "rea" in terms          # 3 letras pero es la senal del pedido
    assert "p2p" in terms
    assert "quiero" not in terms
    assert "version" not in terms
    assert "viejo" not in terms


def test_busqueda_tolera_la_frase_y_descarta_episodios(km):
    real = km.learn(
        content="REA es un servidor MCP: se usa con mcp_call(server='rea').",
        topic_key="rea-analizar-binario", title="REA: como analizar un binario PE",
        category="conocimiento", importance=9,
    )
    real_id = real.get("node_id") or real.get("id")
    episodio = km.learn(
        content=f"Se pidio: {PEDIDO}", topic_key="episodio:abc",
        title="Sesion abc: 1 turnos (exec)", category="episodio", importance=1,
    )
    ep_id = episodio.get("node_id") or episodio.get("id")

    con_episodios = [h["id"] for h in fts_search_tolerant(km, PEDIDO, 10)]
    assert ep_id in con_episodios

    sin_episodios = [h["id"] for h in
                     fts_search_tolerant(km, PEDIDO, 10, exclude_sessions=True)]
    assert real_id in sin_episodios
    assert ep_id not in sin_episodios


def test_memoria_proactiva_inyecta_el_nodo_util(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    from src.core.hybrid_memory import HybridMemoryBackend
    monkeypatch.setattr(HybridMemoryBackend, "_get_embedding", lambda self, text: [])
    reset_knowledge_memory()
    km = get_knowledge_memory()
    km.learn(
        content="REA es un servidor MCP, no una skill. Usar mcp_call.",
        topic_key="rea-analizar-binario", title="REA: como analizar un binario PE",
        category="conocimiento", importance=9,
    )
    km.learn(
        content=f"Se pidio: {PEDIDO}", topic_key="episodio:xyz",
        title="Sesion xyz: 1 turnos (exec)", category="episodio", importance=1,
    )
    loop = AgentLoop.__new__(AgentLoop)
    bloque = asyncio.run(loop._prior_knowledge_block(PEDIDO))
    reset_knowledge_memory()

    assert "MEMORIA DEL GRAFO" in bloque
    assert "REA: como analizar un binario PE" in bloque
    assert "Sesion xyz" not in bloque


def test_memoria_proactiva_pide_investigar_si_no_hay_nada(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_BRAIN", str(tmp_path))
    reset_knowledge_memory()
    loop = AgentLoop.__new__(AgentLoop)
    bloque = asyncio.run(loop._prior_knowledge_block(
        "protocolo inventado zzz-987 que no existe en el grafo"))
    reset_knowledge_memory()
    assert "no hay entradas" in bloque
    assert "learn" in bloque


def test_el_presupuesto_cuenta_los_schemas_de_las_tools():
    pipe = CompactionPipeline({})
    loop = AgentLoop.__new__(AgentLoop)
    loop.compaction = pipe
    loop.compact_threshold = 0.75
    loop._no_recompact_below = {}
    msgs = [{"role": "user", "content": "x" * 2400}]  # ~604 tokens

    loop._tools_tokens_n = 7000
    assert AgentLoop._should_compact(loop, "s1", msgs, 10000) is True

    loop._tools_tokens_n = 0
    assert AgentLoop._should_compact(loop, "s1", msgs, 10000) is False


def test_datos_duros_conservan_los_resultados_de_tools():
    pipe = CompactionPipeline({})
    middle = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "mcp_list", "arguments": {}}}]},
        {"role": "tool", "tool_call_id": "c1",
         "content": "138 tools: open_binary, binary_overview, search_strings"},
    ]
    texto = pipe._datos_duros(middle)
    assert "mcp_list" in texto
    assert "138 tools" in texto
