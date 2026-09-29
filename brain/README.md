# Brain — Nexus v3

Directorio portable `./brain` (configurable via `NEXUS_BRAIN`).

- `knowledge_graph.db` — Knowledge Graph universal (semillas agnosticas, sin rutas locales)
- `rag_index.db` — indice RAG (se genera/actualiza en runtime)

En distribucion el directorio viaja vacio (o con semillas universales); `NEXUS_BRAIN` puede apuntar a `./brain` o a un cerebro externo.

No commitear `*.db` de runtime (gitignore). Documentacion/schema si.
