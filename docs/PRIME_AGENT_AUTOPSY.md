# Autopsia de Prime Agent → qué se adoptó en nexus-v3

Fuente: `https://github.com/PrimeIntellect-ai/prime-agent` (clon shallow en
`%TEMP%\prime-agent`, Rust, ~32 MB de fuente).
Fecha de la revisión: 2026-10-27.
Método: lectura directa de los crates (`pa-core`, `pa-agent`, `pa-cli`, `pa-daemon`, `pa-tui`) y
de los `skills/*/SKILL.md`; no se ejecutó el binario.

---

## 0. Resumen ejecutivo

Prime Agent no es un harness más de "chat + tools": su apuesta es que **el modelo y el harness se
editan a sí mismos** dentro de la misma sesión. Las tres piezas que lo hacen posible son:

| Pieza | Qué es | Dónde vive en prime-agent |
|---|---|---|
| **Kernel RLM** | Un REPL de Python **persistente por sesión** donde el modelo escribe celdas; el "host" media las llamadas del kernel al modelo | `crates/pa-core/src/session_engine/rlm_host.rs` (1.614 líneas), `skills/rlm-heartbeat` |
| **Continual harness / refine** | Al terminar el turno, un planificador analiza la trayectoria y **aplica edits pequeños** (create/update/delete) sobre prompts, memorias, skills y specs de subagente | `refinement/{planner,ranking,executor}.rs`, `session_engine/refine.rs` (1.227 líneas), `skills/refine`, `skills/goal`, `skills/compact` |
| **Cron + heartbeats** | Trabajo diferido real: jobs con schedule (`in/every/at/cron`), store persistente y entrega `Steer`/`FollowUp` que despierta al agente | `crates/pa-core/src/cron/mod.rs` (1.087 líneas) |

Lo que **no** hay que copiar: el provisionamiento de kernels (`kernel/provisioner.rs`, 1.905 líneas,
crea venvs), el cliente MCP con OAuth completo (1.807 líneas), el update installer de binarios
firmados y toda la capa TUI. Son soluciones al problema de "distribuir un binario Rust a terceros",
que nexus-v3 no tiene (es un servicio local en Python sobre el repo del usuario).

---

## 1. Lo que YA se implementó (esta ronda)

### 1.1 `repl` — kernel de Python persistente (`src/core/python_repl.py`)

La idea central de prime-agent, trasladada sin el RLM host: **una sola tool con estado** en vez de
`exec` + scripts sueltos. Con `exec`, cada llamada pierde imports, variables y datos cargados; el
modelo termina escribiendo el mismo `read_csv` tres veces o pagando un `print` gigante para
re-inspeccionar. Con un kernel vivo, carga una vez y sigue trabajando.

Decisiones de diseño (todas con test):

| Decisión | Motivo |
|---|---|
| Protocolo **JSON-lines** por stdin/stdout con marcador `\x1eNEXUS_REPL\x1e` | La celda puede escribir a stdout/stderr libremente sin romper el framing; el padre ignora todo lo que no lleve el marcador |
| `_` = valor de la **última expresión** | Permite `df.shape` y después `_` sin `print`; es lo que hace usable un REPL |
| Timeout **mata el kernel** y avisa que el estado se perdió | Un `while True` no debe colgar el turno. La próxima celda arranca limpia y el mensaje lo dice explícitamente |
| Un kernel **por sesión y por proceso**; reciclado por `idle_s` | Aislamiento entre sesiones sin pagar un proceso por turno |
| La tool es **mutante** (`scope.MUTATING_TOOLS`) y pasa por `scope.check_tool` | No es un sandbox nuevo: corre con los permisos del usuario, igual que `exec` |

Config nueva en `config.yaml` (y en `config.schema.json`): `repl.enabled`, `repl.timeout_s`,
`repl.idle_s`. Con `enabled: false` la tool responde un `PermissionError` explicando por qué.

### 1.2 `git_status` / `git_diff` / `git_log` / `git_commit` (`src/tools/git_tools.py`)

Cierra el gap #3 de `CAPABILITY_MATRIX.md` §5. Antes todo pasaba por `exec`, y eso costaba caro:

- el modelo tenía que escribir el comando exacto (`git -C ... status --porcelain`, `--` antes de
  rutas, comillas en el mensaje de commit);
- con el jail activo, varias formas legítimas se bloquean por encadenamiento (`&&`, pipes);
- un mensaje de commit con comillas o `--force` podía terminar interpretado como flag.

Acá cada operación se arma como **lista de argumentos** (nunca shell) y el mensaje viaja como un
único `-m`. Detalles que se arreglaron durante la implementación, todos con test:

| Síntoma | Causa real | Arreglo |
|---|---|---|
| `NotADirectoryError` con una ruta inexistente | `subprocess` recibía un `cwd` que no existía y explotaba adentro de `Popen` | `repo_root()` valida existencia y tipo antes de invocar git, con mensaje claro |
| "no hay cambios" se reportaba como *error de autor* | `git commit` valida la identidad **antes** de mirar el index | se consulta `git diff --cached --quiet` y se responde `committed: false` sin invocar `commit` |
| "Author identity unknown" crudo | repo sin `user.name`/`user.email` | error en español que dice exactamente qué configurar y aclara que **no se commiteó nada** |
| `-z` con rutas y renombres | parsear líneas rompe con espacios | se parsea `--porcelain=v1 -z` y los renombres se muestran `viejo -> nuevo` |

`git_commit` es **local**: no hace `pull`, `push` ni `reset`. El update del harness sigue siendo de
`self_update.py`.

### 1.3 `run_tests` — runner con parseo de fallos (`src/tools/test_runner.py`)

Cierra el gap #4. `exec` devuelve la salida cruda: el modelo tiene que leer 400 líneas para saber si
fallaron 2 o 200 tests y cuáles. `run_tests` detecta el runner (`pytest` / `npm test` / `cargo` /
`go`), lo corre por argv, y devuelve **estructura**: `ok`, `exit_code`, `counts`
(passed/failed/skipped/error), los primeros fallos con su mensaje, y la salida recortada como
respaldo. Si el proceso no termina, `timed_out: true` con la salida parcial; si falla sin un fallo
identificable, un `hint` que dice que suele ser error de colección/import.

Hallazgo real del parseo: el resumen de pytest **no siempre** viene decorado con `====`. En la
versión que usa el proyecto, con `-q`, la línea final es pelada:

```
1 failed, 1 passed in 0.08s
```

El regex original exigía `^=+ ... =+$` y devolvía `counts: {}` en una corrida real (los tests
unitarios con texto sintético pasaban igual). Ahora se acepta con o sin decoración, se toma la
**última** coincidencia y, si no hay banner, los conteos se infieren de las líneas de progreso
(`.FsEx` con o sin `[ NN%]`).

### 1.4 Bug de checkpoints encontrado de rebote (`src/core/checkpoints.py`)

En la corrida completa, `test_list_corta_a_los_ultimos_n` falló una vez y después pasó siempre. No
era azar del test: el id del checkpoint se armaba con `time.time_ns() % 1_000_000`, y **el reloj
puede devolver el mismo valor en llamadas seguidas**. Dos snapshots del mismo archivo en el mismo
tick compartían nombre → el segundo **pisaba** al primero y se perdía el undo intermedio (justo el
caso que el snapshot existe para cubrir). Ahora se busca un nombre libre (`_v1`, `_v2`, …) antes de
copiar, y hay un test que congela el reloj a propósito.

---

## 2. Mapa completo: prime-agent → nexus-v3

| Mecanismo de prime-agent | Evidencia en el repo | nexus-v3 | Estado |
|---|---|---|---|
| REPL de Python persistente | `session_engine/rlm_host.rs`, `skills/rlm-*` | `src/core/python_repl.py` + tool `repl` | **HECHO** |
| Edits de harness create/update/delete con review | `refinement/{planner,executor}.rs` | — | roadmap §3.1 |
| `/refine` sobre la trayectoria del turno | `session_engine/refine.rs` | — | roadmap §3.1 |
| Cron con `in/every/at/cron` + delivery `Steer/FollowUp` | `cron/mod.rs` | — | roadmap §3.2 |
| Heartbeat por skill (`rlm-heartbeat`) | `skills/rlm-heartbeat` | — | roadmap §3.2 |
| Spawn de subagente que devuelve handle y responde por mensajería | `agent_loop/*`, `pa-agent/src/agent.rs` | `sub_agent_spawner` (continuable, sin mailbox tipado) | parcial |
| Skills como paquetes ejecutables con API en el kernel | `skills/{refine,goal,compact,factory}` | `src/tools/skill_gateway.py` (solo lectura de SKILL.md) | parcial |
| Compactación con presupuesto por rama | `session_engine/compaction.rs` | `src/core/compaction.py` (4 capas) | equivalente |
| Factory DAG de subagentes | `skills/factory` | `task_queue`/`dag_coordinator` | equivalente, no expuesto como skill |
| Provisionamiento de kernels (venvs por tool) | `kernel/provisioner.rs` | — | **no aplica** (Python del proyecto) |
| Cliente MCP con OAuth | `mcp/oauth*.rs` (~1.800 líneas) | `mcp_client.py` (stdio/HTTP, sin OAuth) | no aplica hoy |
| Installer de binarios firmados | `update/installer.rs` | `self_update.py` (git pull ff-only + build) | equivalente |

---

## 3. Roadmap (lo que falta, con punto de aterrizaje)

### 3.1 Continual harness (`refine`)

La pieza más ambiciosa y la que más valor tiene a largo plazo. En prime-agent: al cerrar el turno,
el planificador lee la trayectoria y produce un **plan de edits**; un ejecutor los aplica y un
**review gate** decide si se aceptan (con modo `auto` y umbral). Los edits son pequeños y
versionados, no reescrituras.

Aterrizaje en nexus-v3 (sin copiar el planner en Rust):

1. `src/core/refine.py`: recibe la trayectoria (`event_store` ya tiene los eventos) y devuelve un
   JSON de edits tipados (`create|update|delete` × `prompt|memory|skill|agent_spec`).
2. Aplicación con `checkpoints.snapshot()` antes de cada archivo (ya existe el undo).
3. Gate: `refinement.auto`, `refinement.min_confidence`, `refinement.max_edits_por_turno` en
   `config.yaml`; por defecto **review manual** (propuesta visible en la UI, no auto-aplicada).
4. Historial append-only por scope (global vs proyecto), como `append_global_refinement` de
   prime-agent.

Riesgo principal: auto-editar prompts sin gate degrada el harness en silencio. Por eso el gate
arranca manual y hay que medirlo con `skill_opt`/held-out antes de subir a `auto`.

### 3.2 Cron + heartbeats

nexus-v3 hoy tiene trabajo diferido **ad hoc** (tareas programadas de Windows, `dream_distill`,
`self_learning_loop`). Falta un store de jobs con schedule y entrega tipada.

Aterrizaje:

1. `src/core/cron.py` con tabla en `data/event_store.db`: `id, schedule, prompt, session_id,
   delivery, next_run_at, attempts`.
2. Parser de schedule: `in 10m`, `every 2h`, `at 2026-10-28T09:00`, `cron` (5 campos).
3. Loop de tick en el mismo proceso del server (no un servicio aparte) y entrega `steer` (inyectar
   en el turno en curso) o `follow_up` (encolar un turno nuevo).
4. Tool `cron` + endpoint `/api/cron` para que el modelo y la UI lo manejen.

### 3.3 Mailbox tipado para subagentes

`sub_agent_spawner` ya devuelve un handle continuable, pero la respuesta llega como texto libre.
Falta un buzón con `{from, to, kind, payload}` y que el subagente pueda **pedir** algo a mitad de
tarea (no solo devolver al final). Es lo que hace que prime-agent pueda tener 30 subagentes sin que
el principal se ahogue en texto.

### 3.4 Skills ejecutables

Hoy `skill_gateway` solo lee el `SKILL.md`. Prime-agent permite que una skill registre funciones en
el kernel (`await refine.run(...)`). Aterrizaje natural: que una skill pueda declarar un `main.py`
que el `repl` importe y exponga como objeto (p. ej. `refine`, `goal`, `factory`). Es la extensión que
convierte las skills de documentación en herramientas.

---

## 4. Verificación de esta ronda

| Prueba | Resultado |
|---|---|
| `pytest -q` (suite completa) | exit 0 (incluye 38 tests nuevos en `test_git_tools.py`, `test_test_runner.py`, `test_python_repl.py`) |
| `scripts/e2e_diagnostic.py` contra `:9000` | `total=41 pass=41 fail=0`, exit 0 |
| `scripts/verify_new_tools_mcp.py` (MCP real por stdio, sin LLM) | 52 tools expuestas; `git_status` devuelve rama/head reales, `git_log` los últimos 3 commits, `repl` mantiene estado (`6*7` → `42` en otra celda, mismo pid de kernel), `run_tests` corre pytest y devuelve `passed: 13` |
| Tools listadas por el backend vivo | `GET /api/tools` → 46 tools, incluidas las 6 nuevas |
| Flake de checkpoints | 5 corridas seguidas de `test_checkpoints.py` en verde + test con reloj congelado |

---

## 5. Conclusión

De prime-agent se tomó **la idea que cambia el trabajo diario** (kernel persistente) y se cerraron
los dos gaps de conveniencia que quedaban en la matriz (git y test runner), más un bug real de
checkpoints que apareció en el camino. Lo que queda (refine, cron, mailbox, skills ejecutables) está
documentado con punto de aterrizaje concreto: es trabajo de arquitectura, no de copiar Rust, y se
hace sobre las piezas que nexus-v3 ya tiene (event store, checkpoints, `sub_agent_spawner`,
`skill_gateway`).
