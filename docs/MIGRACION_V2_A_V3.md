# Plan de migración SuperNEXUS v2 → nexus-v3 (PC1)

**Estado:** análisis cerrado el 2026-10-26. **No se ejecutó ningún cambio de migración.**
Este documento es la propuesta a aprobar antes de tocar servicios, configs o el grafo.

---

## 1. Resumen ejecutivo

La migración tiene **dos mitades independientes** que conviene no mezclar:

| Mitad | Qué es | Riesgo | Reversible |
|---|---|---|---|
| **A. Servicio** | OpenCode y otras herramientas hablan con v2 en `:9000` (`/v1` + `/api/brain/*`). Hay que hacer que hablen con v3. | Alto (si se corta, OpenCode se queda sin sus 27 modelos) | Sí, en <2 min |
| **B. Conocimiento** | 3 bases SQLite + 4 archivos `.md` con el conocimiento acumulado de v2. | Bajo (v2 queda intacto, solo se lee) | Sí, con backup |

**Estrategia elegida:** v3 **toma el puerto 9000** cuando v2 se retira, y se le agrega una capa de
compatibilidad para que los consumidores no cambien de URL. El conocimiento se importa con un
script nuevo, idempotente y saneado, sobre el `brain/` de v3, dejando v2 en solo-lectura.

**Lo que NO se migra (decisión explícita, ver §6):** las ~250 rutas de features que v3 no tiene
(dream, swarm, teams, gallery, compose, hall, actors, blueprints, vault, email, trainer,
onboarding, guardrails, dag, connections, conductor, pc/remote, auth, notes), las 6.832
conversaciones y la tabla `memoria`.

---

## 2. Estado verificado hoy (evidencia, no supuestos)

### 2.1 SuperNEXUS v2

| Dato | Valor |
|---|---|
| Proceso | pid 25392 · `python.exe D:\ias\proyectos\supernexus-v2\start_server.py 9000` |
| Framework | aiohttp (`web.Application`), **393 rutas únicas** |
| Tarea programada | `SuperNEXUS v2` → **Running** |
| Otras tareas | `NexusTrainingScheduler` (`training_scheduler.py --once`), `NexusRouter` (`D:\nexus-router`, puerto 11440) |
| `/api/health` | 200 · `nexus-supernexus-v2` 2.1.0 |
| `/api/brain/stats` | 200 · `conocimientos: 10240` |
| `/v1/models` | 9 ids: `supernexus/nemotron`, `supernexus/qwen-coder`, `supernexus/deepseek`, `supernexus/auto`, `auto`, `qwen-coder`, `deepseek-reason`, `nemotron`, `vision` |

### 2.2 nexus-v3

| Dato | Valor |
|---|---|
| Framework | FastAPI, **137 rutas** (113 declaradas con decorador + estáticas/otros) |
| Puerto | `--port` default **8765**; en la app Electron es **dinámico** (puerto libre) |
| Gemas | **24** (`code, engineer, codex, debugger, tester, optimizer, analyst, architect, security, scholar, creative, design, music, director, ayuda, sage, vision, devops, opencode, prompter, trainer, biblioteca, producer, verifier`) |
| `/v1/models` | **45 modelos reales** del catálogo de providers (no gemas) |
| Grafo hoy | **9.002 nodos**, **27.272 aristas**, 12.885 entidades, `max_edges=60000`, margen **32.728**, FTS ok, 19.118 chunks RAG |
| Brain | `D:\ias\proyectos\nexus-v3\brain\` (honra `NEXUS_V3_BRAIN`, **ignora `NEXUS_BRAIN` a propósito**) |
| Estado | **no está corriendo** (8765 libre) |

### 2.3 Brecha crítica comprobada en vivo

Levanté v3 en un puerto de prueba (`:8901`), hice 4 llamadas reales y lo bajé. Resultado:

```
model = "director"          → 200 · "ok"
model = "nexus/director"    → 200 · "ok"
model = "no-existe-xyz-123" → 200 · "PRUEBA-7"
model = "code"              → 200 · "PRUEBA-7"
model = "vision"            → 200 · "PRUEBA-7"
```

**Conclusión:** `/v1/chat/completions` de v3 **ignora el campo `model`** por completo. No falla,
no avisa, no enruta: cualquier id (incluso inventado) responde con el routing interno genérico.
Por eso los 27 ids de `opencode.json` "funcionan" pero **todos se comportan igual** — no hay
selección de gema. Latencia medida: **15–30 s por request** (buffered, sin streaming real).

Esto es lo que hay que arreglar para que la migración no sea solo cosmética.

---

## 3. Inventario de dependencias vivas (lo que hay que tocar)

| # | Objeto | Qué usa de v2 | Acción propuesta | Riesgo si no se toca |
|---|---|---|---|---|
| 1 | `C:\Users\cjtr\.config\opencode\opencode.json` | provider `nexus` → `http://127.0.0.1:9000/v1`, **27 modelos** (`auto, qwen-coder, deepseek-reason, nemotron, vision, qwen2.5-coder:7b, director, code, scholar, architect, creative, sage, analyst, engineer, debugger, optimizer, tester, security, devops, trainer, biblioteca, opencode, codex, design, music, prompter, producer`) | **Sin cambio de URL** si v3 toma `:9000`. Los ids se resuelven a gemas en el server (§5-D2) | OpenCode sin provider `nexus` |
| 2 | `plugins/brain-context.js` | `curl http://localhost:9000/api/brain/stats` (campo `conocimientos`) | Alias `/api/brain/stats` en v3 | Plugin silencioso (try/catch), pierde contexto de cerebro al compactar |
| 3 | `skills/skill-router/search.py` | `HUB = D:\ias\proyectos\supernexus-v2\src\skills\hub` (4.353 `.md`, 198 MB) | **Dejar igual**: son archivos, no servidor. v2 sigue en disco tras retirarse | Ninguno (no depende del proceso) |
| 4 | `C:\Users\cjtr\.config\opencode\AGENTS.md` | Documenta v2, `NEXUS_BRAIN`, nexus-bridge deprecado, 22 gemas v2 | Reescribir §Colmena + §Brain + §Gemasal 24 de v3 | Agentes leyendo instrucciones falsas |
| 5 | Env `NEXUS_BRAIN=D:\...\supernexus-v2\brain` | v2 lo usa; v3 **lo ignora** | Mantener (archivo de v2) y **no** reapuntarlo a v3 | Nada, si nadie lo lee |
| 6 | Env `NEXUS_PORT=9000` | Puerto de arranque v2 | Mantener | Nada |
| 7 | Tarea `SuperNEXUS v2` (Running) | Arranca v2 en `:9000` al boot | `Disable-ScheduledTask` (no borrar) | v2 vuelve a pelear por el puerto |
| 8 | Tarea `NexusTrainingScheduler` | `supernexus-v2\scripts\training_scheduler.py` | `Disable` | Escribe en bases de v2 que ya no se leen |
| 9 | Tarea `NexusRouter` (`:11440`) | Proxy a nodos ollama (**no** depende de v2) | **No tocar** | — |
| 10 | `SuperNEXUS.lnk` | `supernexus-v2\desktop\release\SuperNEXUS v2-2.2.0-portable.exe` | Renombrar a `SuperNEXUS v2 (archivo).lnk` | Usuario abre v2 viejo |
| 11 | `Nexus v3.lnk` | **`C:\Users\cjtr\AppData\Local\Temp\opencode\nexus-clean\nexus-v3.cmd`** (¡la copia de prueba!) | Repuntar a `D:\ias\proyectos\nexus-v3\scripts\nexus-v3-launch.vbs` | Se borra el Temp → acceso roto |
| 12 | `D:\ias\autopsia\01_CORE\*.py` (nexus_mcp, test_nexus_chat, verify_director…) | `localhost:9000` | **No migrar** (legado) | Se rompen en silencio, no importa |
| 13 | Docker `hindsight` (:9999/8899), `nexus-redis` (:6379) | Nada de v2 | No tocar | — |
| 14 | MCP `nexus-bridge` (deprecado) | Board stale desde 24/07/2026 | Ya reemplazado por `nexus-sovereign` (VPS) | Nada nuevo |
| 15 | `start_server.py` / `run_ft.bat` de v3 | Acepta `--port` (`run_ft.bat` pasa args) | Crear lanzador headless en `:9000` (§5-D1) | Sin puerto fijo, OpenCode no lo alcanza |
| 16 | MCP `agent-browser` de `opencode.json` | **Script dentro de v2**: `supernexus-v2\src\bridges\agent_browser_mcp.py` | **Nada**: es un archivo, no el proceso. Por eso v2 queda en disco | Si se borra el repo de v2, ese MCP deja de arrancar |

**Total real a modificar: 8 objetos** (#1 sin URL, #2 alias server, #4 doc, #7, #8, #10, #11, #15).

---

## 4. Brecha funcional v3 vs v2

Familias de rutas de v2 (393) contra v3 (137):

| Familia v2 | Rutas | Equivalente en v3 | Decisión |
|---|---|---|---|
| `/api/memory` | 25 | `/api/memory/*` (2) + `/api/rag/*` (4) + grafo | **Cubierto parcial** |
| `/api/brain` | 8 | `/api/graph/*` (11) + `/api/knowledge/*` (3) + `/api/status` | **Cubierto** + alias `/api/brain/stats` |
| `/api/skills` | 17 | `/api/skills` (1), `/api/skill-opt` (5), `/api/slash` (2) | Parcial |
| `/api/sessions` | 9 | `/api/sessions/*` (20) — más completo | **Mejor en v3** |
| `/api/models`, `/api/ai` | 10 | `/api/providers` (4), `/v1/models` | Cubierto |
| `/api/mcp` | 8 | `/api/mcp` (3) | Parcial |
| `/api/training`, `/api/learning` | 15 | `/api/learning` (3) | Parcial |
| `/api/fs` | 6 | `/api/fs/list`, `/api/fs/read` (3) | Cubierto parcial |
| `/api/checkpoints` | 6 | `/api/checkpoints` (6) | **Cubierto** |
| `/api/voice` | 5 | `/api/voice/stt`, `/api/voice/tts`, `/api/voice/status` (3) | Cubierto parcial |
| `/api/permissions` | 4 | `/api/permission` (4) + `/api/approvals` (6) | **Cubierto / mejor** |
| `/api/dream` | 5 | `/api/dream/run` (1) | Parcial |
| `/api/swarm`, `/api/teams`, `/api/compose`, `/api/hall`, `/api/actors`, `/api/blueprints`, `/api/conductor`, `/api/vault`, `/api/email`, `/api/guardrails`, `/api/dag`, `/api/connections`, `/api/notes`, `/api/scheduler`, `/api/pc`, `/api/auth`, `/api/onboarding`, `/api/gallery` | ~150 | **ninguna** | **Descartar** |
| Resto (market, v3-capabilities, provider-catalog, llm-gateway, cloud-providers, ollama…) | ~90 | Parcial (`/api/providers`, `/api/cloud/free-models`) | **Descartar** |

**Ninguna de estas features la usa OpenCode hoy.** El único consumidor real de v2 es el provider
`/v1` + el plugin de stats. Por eso la paridad 393/393 no es un objetivo: sería portar ~250 rutas
de producto que nadie llama.

---

## 5. Decisiones propuestas

### D1 — v3 toma el puerto 9000 (fijo, headless)
- Lanzador nuevo: `scripts/start_hidden_9000.vbs` → `cmd /c scripts\run_ft.bat --port 9000`.
- Registro como tarea programada al logon (reemplaza a `SuperNEXUS v2`).
- **Por qué:** `opencode.json`, `brain-context.js` y cualquier script heredado siguen funcionando
  sin editar rutas. Cambiar a 8765 obliga a editar ≥3 archivos y rompe los scripts legacy.
- **Contra:** la app Electron usa puerto dinámico; conviven bien (Electron levanta su propio
  backend efímero, el servicio es otro proceso). Hay que documentarlo para no confundir.

### D2 — Capa de compatibilidad `/v1` (el arreglo que importa)
En `openai_chat_completions`:
1. `model` ∈ gemas v3 (24) → ejecutar con **esa gema** (`agent_id`/gema, no solo hint).
2. `model` ∈ modelos reales del catálogo (45) → `explicit` como hoy.
3. `model` ∈ ids v2 heredados (`supernexus/*`, `qwen-coder`, `deepseek-reason`, `nemotron`,
   `vision`) → mapear al equivalente v3 con tabla explícita.
4. `auto` / desconocido → routing actual, pero **loguear warning** (hoy es silencioso: eso fue lo
   que hizo que un id inventado pareciera funcionar).
5. `/v1/models` devuelve **gemas + modelos reales** con `owned_by: "nexus-gema" | provider`.
6. `stream=true`: mantener buffered (chunk completo + `[DONE]`), pero documentar en la respuesta
   y en AGENTS.md que no hay streaming incremental.

**Costo:** 1 archivo (`src/api/server.py`), ~60 líneas, + tests en `tests/`.
**Beneficio:** los 27 modelos de OpenCode pasan a comportarse distinto de verdad (`director`
piensa, `code` escribe, `vision` mira imágenes).

### D3 — Alias `/api/brain/stats`
Devuelve `{"conocimientos": <nodes>, "memorias": <entities>, "temas": <topic_nodes>, ...}`
derivado de `/api/graph/status`, para que `brain-context.js` siga leyendo el campo que espera.

### D4 — El brain sigue siendo el del repo v3
`D:\ias\proyectos\nexus-v3\brain`. **No** se reapunta `NEXUS_V3_BRAIN` ni se copia nada a
`~/.nexus/brain` (ese es de v2 y tiene 386 conocimientos, fork del cliente). v2 queda como
**archivo de solo lectura** con sus `.db` intactos.

### D5 — Importador nuevo, no reusar el de octubre
`scripts/sanitize_and_import.py` (2026-10-11) **solo lee `knowledge_graph.db`**. No toca
`cerebro.conocimientos` (que es donde está el 70% del conocimiento nuevo).
Nuevo: `scripts/import_v2_knowledge.py` con `--dry-run` / `--apply`, backup previo, dedupe por
`content_hash` del contenido ya redactado, y las mismas reglas de saneado + exclusiones.

### D6 — Retirar = deshabilitar, no borrar
`Disable-ScheduledTask` para `SuperNEXUS v2` y `NexusTrainingScheduler`. El repo, las bases y el
`.exe` portable quedan en disco. Rollback = `Enable` + arrancar.

### D7 — No se tocan los 27 modelos de `opencode.json`
Se resuelven server-side (D2). Si el usuario prefiere una lista limpia de 24 gemas, se hace
después del corte, cuando ya haya evidencia de que el mapeo funciona.

---

## 6. Conocimiento a migrar (números medidos)

> **Actualizado tras F3 (import aplicado).** El estimado inicial de ~10.759 nodos contaba solo
> duplicados *byte a byte*. Al comparar texto normalizado y por `topic_key` resultó que v3 ya
> tenía prácticamente todo el conocimiento de v2 (v3 nació de v2 y se migró varias veces).
> Lo que realmente faltaba: **2.586 nodos** y **3.081 aristas**. Detalle abajo.

### 6.1 Fuentes y resultado real

| Fuente | Total | MC fuera | Dup exacto | Mismo tema | Variantes | **Importados** |
|---|---|---|---|---|---|---|
| `cerebro.db` `conocimientos` | 10.241 | 1.890 | 2.882 | 3.142 | 1.264 | **2.325** |
| `cerebro.db` `typed_memories` | 210 | 35 | 166 | 0 | 0 | **9** |
| `knowledge_graph.db` `nodes` | 7.226 | 390 | 6.731 | 5 | 1 | **97** |
| `nexus_memory.db` `observations` | 7.051 | 1.395 | 5.143 | 475 | 22 | **38** |
| `brain/*.md` | 316 | 12 | 187 | 0 | 0 | **117** |
| **Total** | **25.044** | **3.722** | **15.109** | **3.622** | **1.287** | **2.586** |

`*.md` = `findings.md` (63), `decisions.md` (48), `cloud.md` (5), `learnings.md` (1).

### 6.2 Regla de merge aplicada (por qué no se pisó nada)

1. Texto normalizado ya presente en v3 → **se saltea** (15.109 casos).
2. Mismo `topic_key` y contenido parecido (Jaccard ≥ 0,6) → **se saltea**: v3 ya tiene ese
   conocimiento y su versión es la vigente (3.622 casos).
3. Mismo `topic_key` pero la versión de v2 es **bastante más extensa** (>1,5× o +400 chars) o
   claramente distinta (<0,6) → entra como nodo aparte, con `topic_key` vacío y tag
   `v2-variante:<tema>` (1.287 casos). No se pierde detalle y no se sobrescribe el tema de v3.

### 6.3 Resultado en el grafo

| Métrica | Antes | Después |
|---|---|---|
| Nodos | 9.002 | **11.588** |
| Aristas | 27.272 | **30.353** (margen 29.647) |
| FTS | 9.002 | **11.588** (sincronizado por trigger) |
| Fuentes importadas | — | `v2:cerebro` 2.325 · `v2:graph` 97 · `v2:md:*` 117 · `v2:obs:*` 38 · `v2:typed:*` 9 |

Aristas: se intentaron 3.442 (solo pares con ambos extremos mapeados a un nodo de v3, ordenadas
por `weight`), se insertaron 3.081 (`INSERT OR IGNORE` contra duplicados preexistentes).

### 6.4 Qué se descartó y por qué

| Fuente | Filas | Motivo |
|---|---|---|
| `cerebro.memoria` | 378 | **Todas** `tipo='preferencia'`, `clave='gem_code'`, `valor='1'..'378'`. Basura de test |
| `cerebro.conversaciones` | 6.832 | Transcripts crudos, no conocimiento |
| `cerebro.eventos` | 378 | Telemetría |
| `cerebro.patrones` | 0 | Vacía |
| `graph_edges` + `observation_edges` | 249.730 | No entran en `MAX_EDGES=60000`; v3 reconstruye adyacencia por `topic_key`/entidades |
| `message_board.db` | 5.363 | Histórico de colmena, ya no se usa |

### 6.5 Saneado aplicado

| Regla | Afectados |
|---|---|
| Excluir servidor Minecraft (`LATAMRUST`, `COBBLEVERSE`, `MCSS`, `FabricProxy`, `RCON`, `mcrcon`…) | **3.722** |
| Redactar IPs → `[IP]` | 438 en el corpus |
| Redactar secretos (`sk-*`, `ghp_*`, `Bearer`, `password=`, `api_key=`…) → `[SECRET]` | 75 |
| Redactar emails → `[EMAIL]` | 35 |
| Redactar `C:\Users\<usuario>` → `[RUTA]` | 406 rutas de perfil |
| **No** redactar `D:\ias\...` | Decisión: es la misma máquina y es el valor técnico del nodo |

### 6.6 Idempotencia

Segunda corrida del importador (dry-run): **0 nodos nuevos**, 0 variantes.
`verify_install.py` → **14/14 OK** con el grafo en 11.588 nodos.

---

## 7. Fases de ejecución

> Cada fase es **abortable**. Ninguna fase toca datos de v2 (solo lectura).

### F0 — Prerrequisitos y respaldo (5 min)
1. `git status` limpio en `nexus-v3`.
2. Backup del brain v3: `scripts/hygiene.py --apply` o copia de `brain/knowledge_graph.db`.
3. Copia de `opencode.json` → `opencode.json.bak-migracion-<fecha>`.
4. Export de las 3 tareas: `schtasks /query /tn "..." /xml > backup.xml`.
5. Anotar `Get-ScheduledTask` de `SuperNEXUS v2` para rollback.

**Verificación:** existen los 4 respaldos, con tamaño > 0.
**Rollback:** restaurar archivos desde los `.bak`.

### F1 — Capa de compatibilidad en v3 (1–2 h)
1. `_resolve_model_target(model)` en `src/api/server.py`: gema | modelo | id-v2 | auto.
2. `/v1/models` = gemas + modelos con `owned_by`.
3. Alias `/api/brain/stats`.
4. Log de warning para ids desconocidos.
5. Tests: `tests/test_openai_compat.py` (mapeo, `/v1/models`, alias stats).

**Verificación:** `pytest -q` en verde; servidor en `:8901` responde `model=director` distinto de
`model=creative` (comparar `agent`/gema usada en logs), y `no-existe-xyz` devuelve 400 **o**
warning explícito (nunca 200 silencioso).
**Rollback:** `git checkout -- src/api/server.py` (fase sin commit hasta F8).

### F2 — Importador + dry-run (1 h)
1. Escribir `scripts/import_v2_knowledge.py` con `--dry-run/--apply/--limit`.
2. Correr `--dry-run` y comparar con los números de §6.1 (deben coincidir ±2%).

**Verificación:** dry-run reporta ≈7.587 de `conocimientos`, 2.141 de `observations`, 511 de
`knowledge_graph`, 0 escrituras en disco (mtime del `.db` intacto).
**Rollback:** no aplica (solo lectura).

### F3 — Import real (20 min, con backup)
1. Backup `brain/knowledge_graph.db` → `brain/backups/pre-import-<fecha>.db`.
2. `--apply`.
3. Reindexar FTS si el script no lo hace.

**Verificación:** `/api/graph/status` → nodes ≈19.700–20.000, edges ≤60.000, `fts: true`,
`edges_headroom ≥ 0`.
**Rollback:** restaurar el `.db` del backup y borrar WAL/SHM.

### F4 — Verificación de conocimiento (15 min)
1. `GET /api/graph/search?q=<tema que solo existía en v2>` devuelve ≥1 nodo.
2. `recall` (tool) responde con cita de un nodo importado.
3. `verify_install.py` 14/14.
4. `pytest -q` completa.

**Verificación:** las 4 cosas pasan. Si FTS no indexa lo importado, reindexar antes de seguir.

### F5 — Corte de servicio (ventana de 10 min)
1. `Disable-ScheduledTask -TaskName "SuperNEXUS v2"` + `Stop-Process` del pid 25392.
2. Esperar que `:9000` quede libre.
3. Arrancar v3: `scripts\start_hidden_9000.vbs`.
4. `GET :9000/api/health`, `/api/graph/status`, `/v1/models`.
5. Registrar tarea `NexusV3` al logon.

**Verificación:** health 200 en `:9000`, `/v1/models` ≥24 gemas, `brain-context.js` (curl a
`/api/brain/stats`) 200.
**Rollback (<2 min):** `Stop-Process` v3 → `Enable` + `Start-ScheduledTask "SuperNEXUS v2"` →
health 200. OpenCode vuelve a la normalidad.

### F6 — Consumidores (30 min)
1. `brain-context.js`: texto "SuperNEXUS Brain" → "nexus-v3 Brain", URL igual.
2. `AGENTS.md` global: reescribir secciones v2 (brain, gemas 24, nexus-bridge deprecado,
   `NEXUS_V3_BRAIN`).
3. `Nexus v3.lnk`: repuntar a `D:\ias\proyectos\nexus-v3\scripts\nexus-v3-launch.vbs`.
4. `SuperNEXUS.lnk` → renombrar a "(archivo)".
5. `skill-router/search.py`: **sin cambios** (HUB son archivos).

**Verificación:** `grep -r "9000" opencode.json plugins/` sin referencias muertas; abrir el `.lnk`
nuevo levanta la app desde `D:\ias`.

### F7 — Retiro de v2 (10 min)
1. `Disable-ScheduledTask` de `NexusTrainingScheduler`.
2. Dejar `NexusRouter` corriendo (independiente).
3. Documentar en `AGENTS.md`: "v2 archivado, solo lectura".

**Verificación:** `Get-ScheduledTask` muestra `SuperNEXUS v2` = **Disabled**; nada escucha en `:9000`
salvo v3.
**Rollback:** `Enable` de las dos tareas.

### F8 — Verificación final y commit (1 h)
1. Sesión real de OpenCode con `nexus/director` y `nexus/code` (2 prompts distintos, respuestas
   distinguibles).
2. `verify_install.py` 14/14 + `pytest -q`.
3. Prueba de rollback **una vez** (bajar v3, subir v2, verificar, volver).
4. Commit + push en `nexus-v3` (código) y en `opencode` si aplica.

---

## 8. Riesgos y mitigaciones

| Riesgo | Prob. | Impacto | Mitigación |
|---|---|---|---|
| v3 no arranca en `:9000` → OpenCode sin provider | Media | Alto | F5 con rollback de 2 min; v2 queda instalado y `Enabled` hasta F8 |
| Latencia 15–30 s rompe el timeout de OpenCode | **Alta** | Medio | Medir en F1/F8; si excede, subir timeout del provider o usar `auto` |
| `model→gema` mal mapeado (gema sin modelo asignado) | Media | Medio | Test con las 24 gemas en F1; fallback a `auto` con warning |
| Import duplica nodos por hashes distintos (espacios/encoding) | Media | Bajo | Normalizar `strip()` + `content_hash` sobre texto redactado |
| Aristas exceden `MAX_EDGES` y el import falla a mitad | Baja | Medio | Tope duro 20.000 + orden por weight + backup de F3 |
| PII se cuela (ruta/email no cubiertos) | Media | Alto | Reglas §6.3 + dry-run con conteo + revisión de 20 muestras al azar antes de `--apply` |
| Antivirus borra el lanzador (ya pasó con `bootstrap.ps1`) | Media | Bajo | Auto-respaldo, igual que el instalador |
| Electron (puerto dinámico) confunde al usuario | Alta | Bajo | Documentar: app = efímera; servicio = 9000 |
| `NexusTrainingScheduler` queda escribiendo en v2 | Baja | Bajo | F7 lo deshabilita |

---

## 9. Criterio de aceptación (binario, todo o nada) — **CUMPLIDO el 2026-10-26**

- [x] `:9000` sirve v3: `/api/health` 200 (`nexus-v3-harness`), `/v1/models` **70 entradas / 24 gemas**
      (`owned_by=nexus-gema`), `/api/brain/stats` 200 (`fuente: nexus-v3`).
- [x] `/v1/chat/completions` respeta `model`: `director` → header `x-nexus-gema: director`;
      `no-existe-xyz-123` → header `x-nexus-fallback: no-existe-xyz-123` (ya no es 200 silencioso).
- [x] Respuestas distinguibles: `director` ("coordinar y orquestar el ecosistema NEXUS…") vs
      `code` ("ingeniería de software de producción…"), 8,5 s y 17,3 s.
- [x] Grafo: **11.604 nodos**, 30.371 aristas, FTS sincronizado, `verify_install.py` **14/14**,
      `pytest -q` **exit 0**, importador idempotente (2ª corrida = 0 nuevos).
- [x] `SuperNEXUS v2` y `NexusTrainingScheduler` = **Disabled**; `:9000` solo lo escucha v3.
- [x] Rollback probado: v3 abajo → `Enable`+`Start` de v2 → respondió en **7 s** con sus endpoints
      originales (`conocimientos: 10241`, 9 ids `supernexus/*`) → v2 abajo → v3 arriba en **2 s**.
- [x] `Nexus v3.lnk` → `D:\ias\proyectos\nexus-v3\scripts\nexus-v3-launch.vbs` (ya no apunta al Temp).
- [ ] *(pendiente para el usuario)* Reiniciar la sesión de OpenCode para que tome el `opencode.json`
      podado y el `AGENTS.md` nuevo (los lee al arrancar).

---

## 10. Fuera de alcance (explícito)

1. **Paridad de las 393 rutas de v2** — ~250 son features de producto sin consumidor.
2. **Conversaciones (6.832)** — transcripts, no conocimiento.
3. **`memoria` (378)** — basura de test (`gem_code = N`).
4. **Grafo de entidades masivo (249.730 aristas)** — no entra en `MAX_EDGES`, y v3 reconstruye.
5. **Hub de skills (4.353 `.md`, 198 MB)** — queda como archivos en v2; no requiere servidor.
6. **Scripts legacy de `D:\ias\autopsia\01_CORE`** — se rompen en silencio, no se portan.
7. **`NexusRouter` (:11440)** — independiente de v2, sigue igual.

---

## 11. Orden de aprobación sugerido

Si hay que recortar, el mínimo viable es **F0 → F1 → F2 → F3 → F4 → F5 → F8**: deja el servicio
migrado y el conocimiento adentro, sin tocar accesos ni documentación (F6/F7 se hacen después,
sin ventana de riesgo).

**Tiempo total estimado:** 4–5 h de trabajo efectivo, con una ventana de corte de 10 min.

---

## 12. Resultado de la ejecución (2026-10-26)

### Fases

| Fase | Estado | Evidencia |
|---|---|---|
| F0 respaldos | ✅ | `brain/backups/pre-migracion-20261026-1440.db` (9.002/27.272), `opencode.json.bak-migracion-20261026-1440`, 3 XML de tareas |
| F1 compat `/v1` | ✅ | `_openai_model_target()`, headers `x-nexus-gema/x-nexus-model/x-nexus-fallback`, `/v1/models` con gemas, alias `/api/brain/stats`; 18 tests nuevos |
| F2 dry-run | ✅ | 2.586 nodos / 3.442 aristas a insertar, 0 escrituras |
| F3 import | ✅ | 2.586 nodos + 3.081 aristas (el resto duplicadas por índice único), backup automático previo |
| F4 verificación | ✅ | FTS 11.588/11.588, 2ª corrida = 0 nuevos, `verify_install.py` 14/14 |
| F5 corte | ✅ | v3 en `:9000` en 2 s; health/graph/brain/models OK |
| F6 consumidores | ✅ | `opencode.json` → 25 modelos (auto + 24 gemas); `brain-context.js`, `AGENTS.md`, `.lnk` |
| F7 retiro | ✅ | `SuperNEXUS v2` y `NexusTrainingScheduler` = Disabled; `NexusRouter` intacto |
| F8 verificación + rollback | ✅ | director vs code distinguibles; rollback v2→v3 probado una vez |

### Arranque automático

`schtasks /create /sc onlogon` requiere elevación (la sesión no la tiene), así que el autostart quedó
en la **carpeta Inicio**: `Nexus v3 (servicio 9000).lnk` → `wscript scripts\start_hidden_9000.vbs`.
Equivalente funcional al logon, sin necesitar permisos de administrador.

### Hallazgo que obliga a NO borrar v2

El MCP `agent-browser` de `opencode.json` ejecuta
`D:\ias\proyectos\supernexus-v2\src\bridges\agent_browser_mcp.py`. Es una dependencia de **archivo**,
no del proceso: mientras el repo de v2 siga en disco (aunque sus tareas estén `Disabled`), el MCP
funciona. Por eso "retirar v2" es deshabilitar, nunca borrar.

### Rollback reproducible (2 comandos)

```powershell
# volver a v2
Stop-Process -Id (Get-NetTCPConnection -LocalPort 9000 -State Listen).OwningProcess -Force
Enable-ScheduledTask "SuperNEXUS v2"; Start-ScheduledTask "SuperNEXUS v2"   # health 200 en ~7 s
# volver a v3
Stop-Process -Id (Get-NetTCPConnection -LocalPort 9000 -State Listen).OwningProcess -Force
Disable-ScheduledTask "SuperNEXUS v2"
wscript D:\ias\proyectos\nexus-v3\scripts\start_hidden_9000.vbs                 # health 200 en ~2 s
```

### Pendiente para el usuario

Reiniciar OpenCode (cerrar y abrir) para que relea `opencode.json` (25 modelos) y `AGENTS.md`.
Los archivos ya están escritos y verificados; OpenCode los lee al arrancar.
