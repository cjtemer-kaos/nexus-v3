# NEXUS-v3 portable — Bundle MCP + primer arranque

Objetivo: copiar la carpeta a otra PC Windows y quedar operativo
(MCPs + RAG + aprendizaje) desde el primer arranque.

## 1. Qué copiar

| Llevar | Destino | Notas |
|---|---|---|
| Toda la carpeta del proyecto | `D:\ias\proyectos\nexus-v3` (o donde quieras) | Sin rutas absolutas en el código (test `test_portability.py` lo vigila) |
| `data/mcp_bundle/manifest.json` | (incluido) | Catálogo portable de MCPs: comando exacto + env requerida, **sin valores** |
| `data/mcp_bundle/.env.example` | copiar a `.env` y completar | Solo **nombres** de vars; jamás lleva keys reales |
| `brain/` | (se crea solo) | Opcional: enlaza uno externo con `NEXUS_V3_BRAIN=<ruta>` (solo lectura externa, opt-in) |

NO copiar: `node_modules/`, `.venv/`, `*.log`, `data/logs/`, `data/spill/`.

Requisitos previos en la PC nueva: **Python 3.13**, **Node 20+ (npx)**,
**uv/uvx** (para computer-control/serena), **Docker** (solo terraform),
**Ollama** con `nomic-embed-text` (`ollama pull nomic-embed-text`) y al
menos un modelo de chat.

## 2. Primer arranque paso a paso

```powershell
cd <carpeta-del-proyecto>
copy data\mcp_bundle\.env.example .env   # completar keys (ver §3)
ollama pull nomic-embed-text
python -c "from src.core.mcp_setup import run_mcp_setup; print(run_mcp_setup(fix=False)['summary'])"
python -c "from src.core.firstboot import ensure_rag_ready, ensure_learning; print(ensure_rag_ready()); print(ensure_learning())"
python -m pytest tests/test_mcp_setup.py tests/test_firstboot.py tests/test_portability.py -q
```

Desde el CLI de setup (otro agente): **solo import/llamada**, no edites su CLI:

```python
from src.core.mcp_setup import run_mcp_setup          # fix=True instala lo instalable
from src.core.firstboot import ensure_rag_ready, ensure_learning, firstboot_report
report = firstboot_report(fix_mcp=True)               # dict unico: mcp + rag + learning
```

## 3. Qué pide (keys/cuentas) y qué es automático

**Automático / cero-config** (con `fix=True` se instalan solos):
playwright, chrome-devtools (requiere Chrome con
`--remote-debugging-port=9222`), context7, rea, computer-control
(requiere uv + Python 3.13), serena (deshabilitado por defecto),
hindsight (opcional; si no corre en `:8888` queda no disponible sin bloquear).

**Pide key o cuenta** (quedan en `needs_key` hasta completar `.env`):
`BRAVE_API_KEY` (brave-search), `GITHUB_PERSONAL_ACCESS_TOKEN` (github),
`GREPTILE_API_KEY` (greptile, deshabilitado), `TFE_TOKEN` + Docker
(terraform, deshabilitado), `DISCORD_TOKEN` + `ALLOWED_GUILDS`
(discord: crear app/bot en el portal), OAuth en el cliente
(rube/gitlab/linear/cloudflare/asana, todos deshabilitados),
`firebase login` previo (firebase, deshabilitado).

**Manuales** (ruta local no portable; ver `run_mcp_setup()['manual']`):
agent-browser, cua-driver, qwen-mm-* — define `NEXUS_AGENT_BROWSER_CMD`,
`NEXUS_CUA_DRIVER_BIN` o `NEXUS_QWEN_MM_DIR` en esta PC.

**RAG/aprendizaje**: `ensure_rag_ready()` resuelve el modelo de embeddings
de ESTA pc (`nomic-embed-text` o familia), crea `rag_index.db` si falta y
hace smoke test. `ensure_learning()` crea el brain, verifica el grafo
(upsert por `topic_key`), ejecuta `maintain()` y comprueba gates
(Thompson + held-out fail-closed + mantenimiento cada 6h).
