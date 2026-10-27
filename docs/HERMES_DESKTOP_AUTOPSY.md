# Hermes Agent Desktop — autopsia funcional

Análisis del escritorio de Hermes Agent (Nous Research, v0.22.5) hecho sobre la app **corriendo
en esta máquina** y sobre su código fuente instalado, para decidir qué conviene llevar a nexus-v3
y qué no.

- **Fecha**: 2026-10-27
- **App corriendo**: pid 219992, ventana `Hermes` (hwnd 526986), 1920x1032,
  ruta `#/20260906_145139_3d4431`
- **Empaquetado**: `apps\desktop\release\win-unpacked\Hermes.exe`
  (`resources\app.asar.unpacked\dist\index.html`)
- **Fuente completa**: `C:\Users\cjtr\AppData\Local\hermes\hermes-agent\`
  (repo con backend Python, gateway, plugins, skills, TUI, web y el escritorio)
- **Screenshot real**: `C:\Users\cjtr\AppData\Local\Temp\opencode\hermes_win.png`

Este documento describe **cómo funciona**, no sólo cómo se ve. La sección 7 lista lo adoptable,
la 8 lo que no conviene copiar y la 9 la paridad contra nexus-v3.

---

## 1. Qué es

Hermes Desktop **no es un dashboard web embebido ni una TUI envuelta**. Es una superficie de chat
nativa propia, con tres autoridades separadas y un contrato explícito entre ellas
(`apps/desktop/AGENTS.md`):

| Autoridad | Dueña de | Nunca hace |
|---|---|---|
| **Electron (main)** | ciclo de vida del proceso, filesystem/git/ventanas nativas, instalación y update, bridge tipado de capacidades | lógica de agente, render de UI |
| **Renderer (React)** | navegación, presentación, estado efímero de interacción | tocar Node, hablar con el FS directamente |
| **Backend de agente (Python)** | sesiones, tools, llamadas a modelo, streaming | pintar nada |

La regla dura: *el renderer nunca alcanza Node ni Electron directo*. Todo pasa por
`window.hermesDesktop` (un `contextBridge`), y del otro lado el backend se habla por un socket
JSON-RPC. Esa separación es la razón por la que el mismo renderer puede apuntar a un backend
local, a uno remoto por SSH o a un gateway compartido sin cambiar una línea de UI.

### Escala real (medida en el fuente instalado)

| Capa | Archivos | Líneas |
|---|---|---|
| `gateway/` (Python) | 326 | 109.794 |
| `tui_gateway/` (Python) | 118 | 38.662 |
| `agent/` (Python) | 394 | 125.378 |
| `hermes_cli/` (Python) | 762 | 253.143 |
| `apps/desktop/src` (TS/TSX) | 3.192 | 597.255 |
| `apps/desktop/electron` (TS) | 648 | 118.078 |

No es un proyecto chico: el escritorio solo son ~715.000 líneas de TypeScript, y el contrato
entre capas está **generado**, no escrito a mano (ver 3.1).

---

## 2. Mapa de procesos

```
Hermes.exe (main, Electron)
 ├─ gpu-process / renderer (Chromium)      ← 1920x1032, hash router
 ├─ utility processes
 └─ gateway Python (hermes gateway / tui_gateway)   ← JSON-RPC sobre WebSocket
      ├─ sesiones (SQLite por perfil)
      ├─ agentes + tools + MCP
      ├─ cron / watchers / delivery
      └─ hosted rooms (multi-agente / kanban)
```

### 2.1 Arranque (bootstrap)

El main no asume que el backend esté sano; lo negocia. Los módulos del orden de arranque son
explícitos: `bootstrap-runner.ts` (30 KB), `bootstrap-repair-guard.ts`, `bootstrap-failure-copy.ts`,
`bootstrap-platform.ts`, `bootstrap-state.ts`, `install-stamp.ts`, `first-run-setup-gate.ts`.

Pasos que se deducen de esa superficie:

1. **Stamp de instalación** — `install-stamp.ts` decide si esta copia es nueva/actualizada.
2. **Descubrimiento** — `backend-discovery.ts`, `backend-descriptor-cache.ts`,
   `backend-probes.ts`, `backend-health.ts` (12,7 KB) buscan un backend ya escuchando antes de
   lanzar uno. Si hay uno vivo y compatible, se adopta.
3. **Resolución** — `backend-resolution.ts`, `backend-ownership.ts`, `backend-claim.ts`,
   `backend-dial-claim.ts`, `host-backend-singleton.ts`, `singleton-lock.ts`: un solo dueño por
   backend; dos ventanas no lanzan dos servidores.
4. **Spawn si hace falta** — `local-gateway.ts` (32,9 KB), `local-gateway-python.ts`,
   `backend-child.ts`, `backend-env.ts`, `venv-holder-select.ts`, `source-python.ts`. El
   intérprete se elige de un venv conocido; hay variante Windows
   (`runtime_bootstrap_windows.py`, `windows-hermes-path.ts`, `windows-child-options.ts`).
5. **Espera de readiness** — `backend-ready.ts` (11,2 KB) con timeout, progreso
   (`getBootProgress`) y estado publicado a la UI (`Backend listo` en la barra de estado del
   screenshot).
6. **Reparación** — si el backend no arranca: `bootstrap-repair-guard.ts`, `bootstrap-runner.ts`,
   `continueBootstrapLocal`, `resetBootstrap`, `repairBootstrap`, `cancelBootstrap` (los cuatro
   expuestos por IPC).
7. **Recycle/release** — `backend-recycle.ts`, `backend-release-gate.ts`,
   `local-backend-lifecycle.ts`, `quit-teardown.ts`, `close-stop-kill.ts`.

Lo importante del diseño: **el estado de arranque es un objeto consultable** (`getBootstrapState`)
y cada falla tiene copy propio (`bootstrap-failure-copy.ts`), no un alert genérico.

### 2.2 Conexiones (local, remoto, multi-perfil)

Hay dos niveles distintos que conviene no confundir:

- **Conexiones** (`connection-registry.ts`, 61,5 KB; `connection-config.ts`, 43,1 KB;
  `connection-apply.ts`, `connection-caches.ts`, `connection-window-state.ts`,
  `connection-route-identity.ts`): a qué backend apunta cada ventana. Tipos: local, remoto por
  SSH (`ssh-connection.ts` 45,5 KB, `ssh-bootstrap-coordinator.ts`, `ssh-control-master-holders.ts`,
  `ssh-isolated-keepalive.ts`), gateway compartido / cloud (`remote-lifecycle.ts` 65,4 KB,
  `remote-liveness.ts`, `cloud-boot-cascade.ts`, `cloud-discovery.ts`).
- **Perfiles** (`desktop-profile.ts`, `profile-route*.ts`, `profile-migration.ts`,
  `profile-migration` + `profile-id-guard.ts`): un perfil es un espacio de datos **aislado**
  (sesiones, skills, MCP, config). Es multi-tenant local, no un simple filtro de la lista.

El renderer pide "la conexión del perfil X" y recibe un descriptor; nunca construye una URL a
mano. `window-connection-route.ts`, `profile-session-routing.ts` (55,8 KB) y
`plugin-profile-routes.ts` resuelven el ruteo por ventana.

### 2.3 Bridge de capacidades

`preload.ts` (38,8 KB) expone **un solo objeto**, `window.hermesDesktop`, con `ipcRenderer.invoke`
por capacidad. Muestra del contrato (no exhaustiva):

| Grupo | Ejemplos |
|---|---|
| Conexión | `getConnection`, `revalidateConnection`, `getEmbedHostOrigin` |
| Ventanas | `openWindow`, `openSessionWindow`, `openSessionInTerminal`, `readWindowBelow` |
| FS / git | `writeTextFile`, `renamePath`, `desktopPluginsRoot`, `reconcileDesktopPlugins`, `logsRoot`, `readPluginSource` |
| Portapapeles / captura | `readClipboard`, `saveClipboardImage`, `contextMenuCopyImage`, `saveImageBuffer`, `requestMicrophoneAccess` |
| Preview | `normalizePreviewTarget`, `setPreviewGuestHidden` |
| Logs | `revealLogs`, `getRecentLogs`, `logLine` |
| Arranque | `getBootstrapState`, `probeLocalBackend`, `continueBootstrapLocal`, `resetBootstrap`, `repairBootstrap`, `cancelBootstrap` |
| Máquina | `getMachineProfile`, `getOnBattery`, `getVersion`, `getSecretStorageEncryption`, `sshConfigHosts` |
| Ventana | `findInPage`, `stopFindInPage`, zoom, controles de título |

Cada `invoke` tiene su handler en `electron/`, muchos con archivo propio y test
(`fs-ipc.ts`, `git-ipc.ts`, `terminal-ipc.ts`, `clipboard-ipc.ts`, `hud-ipc.ts`, ...). El bridge
es **angosto y tipado**: no hay `ipcRenderer` genérico ni `nodeIntegration`.

### 2.4 Lo que el main también resuelve

- **Update con handoff**: `updater/` (24 módulos: `channel*`, `checkout*`, `marker*`, `relaunch*`,
  `operation`, `state-db-preflight`, `store*`, `update-metrics`), más `app-updater.ts`,
  `update-gate.ts`, `update-marker.ts` (26 KB), `update-marker-judge.ts`,
  `update-hold-wiring.ts`, `gateway-stop-before-update.ts`, `gateway-download-transport.ts`.
  Actualizar implica: bajar, verificar, parar el gateway, migrar el state-db, relanzar y esperar.
- **Salud del renderer**: `renderer-crash-metrics.ts`, `renderer-heap-flags.ts`,
  `renderer-log.ts`, `main-process-lag-watchdog.ts`, `startup-latency-ipc.ts`,
  `crash-forensics.ts`, `shutdown-forensics` (Python).
- **Ventanas especiales**: HUD de juego (`hud-*.ts`, 15 módulos), pet/overlay
  (`pet-overlay.ts`, `pet-overlay-ipc.ts`), quick entry (`quick-entry.ts`),
  wake indicator (`wake-indicator*.ts`), título de link (`link-title-*.ts`).
- **Hardening**: `hardening.ts` (22,8 KB), `secret-storage-policy.ts`, `native-token-store.ts`,
  `oauth-partition.ts`, `hub-iframe-policy.ts`, `preview-guest-escape.ts`,
  `window-open-policy.ts`, `external-open.ts`.

---

## 3. El protocolo: JSON-RPC sobre WebSocket

Es la pieza más replicable de todo el escritorio. El backend expone un **gateway** (`tui_gateway`)
que habla JSON-RPC por WebSocket; la misma superficie la usan el escritorio, la TUI (Ink) y el
dashboard web. No hay endpoints REST ad-hoc por feature.

### 3.1 El contrato está generado desde Python

`apps/shared/src/gateway-contract.generated.ts` son **7.270 líneas** con cabecera:

```
// GENERATED by scripts/gen_gateway_contracts.py from tui_gateway/contracts — DO NOT EDIT.
// tests/tui_gateway/contracts/test_generated.py fails when this file is stale.
```

Ese archivo define, para cada método: params y result; y además `ServerRequestMap`
(server→client) y `GatewayEventMap` (notificación → payload). Consecuencias:

- **Python es la única fuente de verdad.** Un campo que el backend deja de mandar rompe `tsc`,
  en vez de derivar en `undefined` silencioso en runtime.
- El test de frescura impide que el contrato quede viejo.
- Los consumidores (TUI, desktop, web) no pueden divergir entre sí.

Métrica: **392 métodos**, agrupados en 24 prefijos:

| Grupo | # | Grupo | # | Grupo | # |
|---|---|---|---|---|---|
| `session` | 53 | `image` | 5 | `setup` | 3 |
| `pet` | 18 | `subscription` | 5 | `command` | 3 |
| `projects` | 16 | `tool` | 5 | `skills` | 3 |
| `display` | 14 | `onboarding` | 5 | `handoff` | 3 |
| `connectors` | 11 | `approval` | 5 | `model` | 3 |
| `mcp` | 11 | `config` | 5 | `clarify` | 3 |
| `shared_metrics` | 10 | `rollback` | 4 | `tools` | 3 |
| `subagent` | 10 | `learning` | 4 | `plugins` | 3 |
| `vault` | 10 | `moa` | 4 | `terminal` | 3 |
| `prompt` | 9 | `process` | 3 | `theme`/`shell`/`agent`/... | 1-2 |
| `browser` | 8 | `worker` | 3 | | |
| `wake` / `preview` / `billing` / `bot_relay` / `voice` / `profiles` | 7 c/u | `spawn_tree` / `connection` | 3 | | |

### 3.2 Ciclo de un turno

Métodos del núcleo (`session.*` y `prompt.*`):

```
session.create / session.resume / session.activate / session.list / session.most_recent
session.history / session.info / session.status / session.usage / session.context_breakdown
session.events.since / session.events.stats        ← replay incremental
prompt.submit / prompt.cancel / prompt.receipt
prompt.steer_queued / prompt.replace_queued        ← mensaje encolado mientras el turno corre
prompt.background / prompt.btw / prompt.resolve_unknown
session.steer / session.interrupt / session.redirect   ← intervenir un turno en curso
```

Eventos que el backend empuja durante el turno:

```
message.start → message.delta → message.interim → message.complete
tool.start → tool.generating → tool.progress → tool.complete → tool.output_risk
subagent.start → subagent.progress → subagent.thinking → subagent.tool → subagent.complete
todo.updated · thinking · agent.terminal.output
approval.request / clarify.request (ver 3.3)
```

Detalle fino del cliente (`src/api/client.ts`):

- `prompt.submit` es *fire-and-forget*: el fin del turno lo marcan los eventos, no el ACK. Por eso
  su timeout es **1.800.000 ms** (30 min), igual al techo del turno del backend. Si se dejara el
  default de 30 s aparecería un toast de "timeout" mientras el turno sigue vivo.
- Los métodos marcados `ATTACH_REQUIRED` (`prompt.submit`, `approval.respond`, `clarify.respond`,
  `session.interrupt`, `prompt.cancel`, `session.rewind`, `prompt.steer_queued`,
  `prompt.replace_queued`) exigen un attach previo: la mutación viaja con la revisión de la sesión.
- `PARENT_ATTACH_REQUIRED` (`session.branch_stored`, `session.branch_whole`,
  `session.workspace.move`) hacen attach del **padre** primero: ramificar es una mutación CAS
  sobre la sesión origen, y una fila del sidebar que nunca se abrió no tiene revisión cacheada.

### 3.3 Aprobaciones y preguntas: requests, no eventos

Hay dos caminos y el cliente los unifica:

- **Legacy** (`hermes serve`): `approval.pending`, `approval.received`, `approval.respond`,
  `clarify.lock`, `clarify.respond` como métodos normales.
- **Canónico** (gateway nuevo): el backend emite **eventos** `approval.request` / `clarify.request`
  con `prompt_id` y `execution_generation`; el cliente los proyecta a los *named request listeners*
  y responde con `approval.respond` / `clarify.respond`.

`client.ts` documenta el motivo: el renderer sólo conoce los *requests* server→client, así que
cada prompt entrante se entrega al mismo registry `onRequest` como si fuera un request cuyo
`respond` emite el RPC. **Una sola UI para aprobar, dos transportes posibles.**

### 3.4 Replay, huecos y multi-socket

Cada evento lleva `seq` (contador monotónico por sesión, estampado por
`tui_gateway/event_replay.py::_stamp_event`) y `replay_epoch`. Cuando el socket se reconecta:

- `session.events.since` recupera desde la última `seq` conocida;
- si el epoch cambió o el ring se truncó, el cliente sintetiza `session.replay_gap`
  (`{latest_seq, replay_epoch}`) y **re-resume la sesión completa** para tener snapshot;
- los eventos recuperados se marcan `replayed: true` para no confundirlos con trabajo nuevo.

Además `replayEpoch` + `connectionId` + `profile` acompañan cada evento, así que un renderer con
dos sockets al mismo proceso (dos ventanas, o dos perfiles) reconoce el mismo frame duplicado y lo
deduplica (`event-dedupe.ts`).

### 3.5 Compatibilidad entre versiones

`LEGACY_STRIP` define qué claves se quitan cuando el dial es legacy, porque un backend viejo
rechaza claves desconocidas como *version skew* (`4000`). Ejemplo documentado: `session.create`
pierde `request_id`; `session.branch` pierde `through_message_id`. `prompt.submit` **conserva**
`submission_id` a propósito, porque el camino de submit reintenta sin identidad ante ese rechazo
exacto. Es una decisión de compatibilidad escrita en el código, no un `try/except` suelto.

### 3.6 Anti-fragmentación

`canonical-protocol.ts` (42 KB) normaliza: `canonicalProfile`, `canonicalSessionKey`,
`canonicalOwnerProfile`, `canonicalPromptParams`, `slashMutation`, `pickerModelMutation`,
`splitRewindSubmit`. Todo lo que cambia estado pasa por ahí. Eso evita el clásico "cada pantalla
inventa su payload".

---

## 4. Superficie de UI: 12 rutas, dos familias

Router por hash (`#/<id>`), definido a mano en `src/app/routes.ts` (no hay `react-router`
declarativo). Las rutas se clasifican en **overlays** (modal a pantalla completa sobre el chat) y
**páginas de workspace** (renderizan dentro del panel de trabajo, con el chat detrás).

| Ruta | Vista | Familia | Qué hace |
|---|---|---|---|
| `/` y `/<sessionId>` | `chat` | workspace | el chat; `/<id>` es la sesión abierta |
| `/settings` | `settings` | overlay | ajustes completos (billing, vault, delegación, memoria, modelos locales) |
| `/command-center` | `command-center` | overlay | logs, estado, analytics de uso, reiniciar gateway, update, mantenimiento |
| `/capabilities` | `capabilities` | workspace | catálogo: MCP servers, toolsets/tools, skills, plugins |
| `/messaging` | `messaging` | workspace | plataformas de mensajería + allowlist + QR de Telegram |
| `/artifacts` | `artifacts` | workspace | artefactos generados por las sesiones |
| `/cron` | `cron` | overlay | trabajos programados + blueprints |
| `/profiles` | `profiles` | overlay | crear/renombrar/borrar perfiles aislados |
| `/agents` | `agents` | overlay | roster de agentes/dispositivos |
| `/starmap` | `starmap` | overlay | grafo de memoria + skills (visualización) |
| `/webhooks` | `webhooks` | overlay | webhooks entrantes |
| `/session-import` | `session-import` | overlay | importar sesiones de otros agentes |

Extras:

- **Rutas contribuidas**: plugins montan una página completa en su propio path (`routes` es un
  *registry area*). Los paths contribuidos se reservan igual que los nativos para que el parser de
  `sessionId` no los confunda con una sesión; hay una vista `extension` aparte justamente para que
  el sidebar no marque una sesión y el titlebar no muestre el dropdown de sesión.
- **Overlays vs páginas**: mientras un overlay está abierto, los *titlebar clusters* se ocultan
  (`hidesFixedTitlebarClusters`), porque viven en un z-index mayor y sangrarían sobre la tarjeta.
- **Re-click**: ir a una página que ya está en pantalla no cambia la location, así que no dispara
  efecto; por eso existe `navigateToWorkspacePage`, que además *frentea* el panel de trabajo.
- **Query stripping**: `routePathname` corta `?`/`#` antes de decidir la vista. Sin eso,
  `/capabilities?tab=connectors` se leía como la sesión `skills?tab=connectors`.

### 4.1 El chat: lo más denso del producto

`src/app/chat/` tiene 203 archivos. Lo que hace, agrupado:

**Composer**
- draft persistente por sesión, undo de composición (`undo-history.ts`), historial de input
  (`composer-input-history.ts`), popout del composer a ventana aparte.
- **cola de mensajes** mientras el turno corre (`queue-slot.tsx`, `queue-panel.tsx`,
  `composer-queue.ts`) con *steer* (redirigir el turno en curso) en vez de esperar.
- adjuntos con drag&drop, paste grande a archivo (`large-paste.ts`), recuperación de imagen
  preparada, screenshots del composer.
- referencias tipadas: `@` archivos (`use-at-completions`, `path-refs.ts`), `/` slash
  (`use-slash-completions`), URLs (`url-refs.ts`, `use-composer-url-dialog`), inline refs.
- voz: dictado, conversación live, auto-speak de respuestas (`use-voice-*`), selector de motor.
- barra de estados del composer: modelo (pill), esfuerzo de razonamiento (pill), conexión
  (glyph), modo de aprobación, uso de contexto.

**Transcript**
- streaming con flush por delta (`stream-delta-flush.ts`), ventana de transcript con retención y
  backfill (`transcript-window.ts`, `transcript-retention.ts`, `transcript-backfill.ts`),
  cache del tail.
- filas de tool con progreso, payload técnico colapsable, dismiss, "tool drafting".
- sub-agentes con transcript propio y controles (interrupt/steer/tail).
- todos, thinking, reacciones, marcador de "model changed", separador de turno interrumpido.
- restore de aprobación/pregunta pendiente al reabrir (`restore-pending-approval.ts`,
  `restore-pending-clarify.ts`, `restore-pending-connection.ts`).

**Sesiones (sidebar)**
- grupos por proyecto/workspace, arrastrar sesión a otro grupo, virtualización de lista.
- búsqueda server-side, pin, archivar, unread local+remoto, dot de estado, densidad configurable.
- acciones por fila: branch, fork, export, mover de workspace, abrir en ventana nueva, abrir en
  terminal, copiar link.

**Navegación del chat**
- `branch` desde un mensaje (bifurca la conversación), `rewind` (volver atrás y reenviar),
  `undo`, `compress`, `steer`, `interrupt`, `redirect`.

### 4.2 Panes: workbench con zonas y tiles

`src/components/pane-shell/` (35 módulos) implementa un **workbench tileable**:

- `grid-model.ts`, `grid-to-tree.ts`, `zones-engine.ts`, `track-model.ts`, `geometry.ts`:
  modelo de grilla con zonas (left/center/right/bottom), divisores y tracks.
- `tree-split.tsx`, `tree-node.tsx`, `tree-group.tsx`: render en árbol de splits.
- `presets.ts`, `preset-tree.ts`, `layout-picker.tsx`: layouts guardados ("coding", "review", ...).
- `parked-panes.ts`, `floating-panes.tsx`, `keep-alive-panes.tsx`, `narrow-overlays.tsx`:
  un pane aparcado no se desmonta (keep-alive), y en ventana angosta se convierte en overlay.
- `pane-lifecycle.ts`, `pane-visibility.ts`, `tab-selection.ts`, `tab-strip-scroll.ts`,
  `strip-visibility.ts`: ciclo de vida, visibilidad y tabs de cada pane.

Panes concretos: **chat**, **files**, **review** (diff), **terminal**, **preview** (navegador
embebido con anotación), **browser popout**. El agente puede pedir foco de pane
(`focus_pane`), así que la UI y el agente comparten el mismo concepto de panel.

### 4.3 Estado del renderer

Nanostores (átomos) con **un store por concepto**: `src/store/` tiene 189 módulos. Los
que definen la forma del producto:

| Store | Responsabilidad |
|---|---|
| `session.ts`, `session-states.ts`, `session-sync.ts`, `session-owner-*` | sesiones, dueño por conexión/perfil, resolución de owner |
| `transcript-tail.ts`, `transcript-sync.ts`, `thread-scroll.ts`, `thread-timeline.ts` | transcript y scroll |
| `composer*.ts`, `pending-submissions.ts`, `composer-queue.ts` | composición y cola |
| `approval-mode.ts`, `server-requests.ts`, `clarify.ts`, `confirm.ts` | intervención humana |
| `interface-mode.ts`, `layout.ts`, `panes.ts`, `tabstrip-prefs.ts` | presentación |
| `gateway.ts`, `gateway-reconnect.ts`, `gateway-switch.ts`, `connections.ts` | transporte |
| `free-tier.ts`, `billing-block.ts`, `model-pricing.ts` | comercial |
| `pet.ts`, `hud.ts`, `wake-word.ts`, `vibe-hearts-enabled.ts` | gamificación |

Dos patrones que vale copiar:

1. **Modo de interfaz como *resolver input*, no como preset.**
   `effective(surface) = sessionReveal ?? policy[mode] ?? userPreference`.
   `advanced` tiene policy vacía (cae al gusto del usuario); `simple` *sombrea* la preferencia en
   vez de sobrescribirla. Consecuencia: un toggle hecho en `simple` vale para la sesión y no
   contamina la preferencia del otro modo. Los consumidores nunca conocen la palabra "modo":
   leen un átomo "efectivo".
2. **Modo de aprobación por perfil**: `manual | smart | off`, sincronizado contra el gateway y
   *optimista* hasta que el socket abre (con guarda para no sincronizar antes de `open`).

### 4.4 Diseño (DESIGN.md)

Contrato corto y ejecutable, no un manual de marca:

- **Flat, not boxed**: nada de card dentro de card ni divisores por todos lados.
- **Tokens sobre literales; una fuente por concepto**. Si hace falta un color crudo, una sombra
  de una sola vez o un `px-*` suelto en un control, ya existe la primitiva.
- **Named contracts** (tokens, variantes de `Button`, nombres de primitivas) se mantienen *con* el
  código: cambiar una primitiva obliga a actualizar su entrada en el mismo cambio; un nombre
  viejo en el doc es un bug igual que un tipo viejo.
- **Intención antes de automatización**: la app no adivina; si algo va a pasar solo, se dice.
- Overlays para lo transitorio, panes para lo que convive.

Lo aplican con `components/ui/` (primitivas) + `components/pane-shell` (layout) y con
`themes/` + `theme-presets` (15,9 KB en shared) para temas.

---

## 5. Las páginas, una por una

### 5.1 `/capabilities` — el catálogo

La página más ancha después del chat (99 archivos). Cuatro pestañas que comparten un mismo
patrón de catálogo:

- **MCP servers**: alta manual (forma con draft aparte: `add-server-draft.ts`,
  `use-mcp-draft.ts`), alta desde catálogo, probar servidor (`use-mcp-probes.ts`), logs del
  servidor (`mcp-logs.tsx`), estado (`mcp-status.ts`), control del servidor local
  (`local-server-control.tsx`, `local-slots.tsx`), aliases, y **hosted** (servidores alojados con
  aviso y confirmación propios).
- **Toolsets / tools**: lista con filtros (`use-tools-editor.ts`, `tools-filter-bar.tsx`),
  contadores (`tools-summary.tsx`), editor por tool, detalle de toolset, config de toolset
  (`toolset-config-panel.tsx` en settings).
- **Skills**: catálogo (`skill-catalog.tsx`), detalle (`skill-detail.tsx`), procedencia
  (`skill-provenance.ts`), instalación desde catálogo, "update skills", y detalle de skill
  oficial.
- **Plugins**: paquetes (`plugin-packages.ts`), instalar/actualizar (`plugin-update.ts`), panel de
  tool que aporta cada plugin (`plugin-tools-panel.tsx`), form de ajustes que el plugin declara.

Transversal: búsqueda unificada (`capability-search.tsx`), derivación de la página desde la URL
(`derive.ts`, `derive-page.ts`, `derive-tools.ts`), deep links (`deep-link.ts`), prefetch
(`prefetch.ts`), y **"ways section"** (`ways-section.tsx`): un bloque de *formas de usar* esta
capacidad — el puente entre "instalé esto" y "para qué me sirve".

### 5.2 `/starmap` — grafo de memoria y skills

Visualización con `d3-force` de los nodos de **memoria** (rombo) y **skill** (círculo), en un
disco inclinado (`TILT = 1`, "mirando un disco en perspectiva") con anillos de recencia:

- `simulation.ts` (force layout), `render.ts` (canvas), `geometry.ts`, `color.ts`,
  `time-axis.ts` + `timeline.tsx` (línea de tiempo con playback y hotkeys), `share-code.ts` +
  `share-controls.tsx` (compartir una vista), `node-context-menu.tsx`.
- Gradiente de edad: lo viejo apagado (`oldInk 0.42`), lo nuevo brillante (`newInk 0.95`),
  radio anclado en el tiempo (`tr`), anillos por banda de recencia.
- `AGE_GRADIENT`, `NODE_SHAPE`, `ORB_DARKEN`, `WHITEISH_SHEEN`: la estética está parametrizada en
  constantes, no hardcodeada en el render.

Es la misma idea que el grafo de nexus-v3, pero **como pieza de producto**: se puede reproducir el
tiempo, compartir un estado y navegar con menú contextual.

### 5.3 `/command-center` — operación

Mezcla dos mitades:

- **Estado y logs**: `getStatus`, `getLogs` con búsqueda y tail en vivo (`LogTail`,
  `LogSearchField`), tabs responsivas, reiniciar gateway (`confirmSharedGatewayRestart`), update
  de Hermes, analytics de uso (`getUsageAnalytics`).
- **Sesiones**: exportar, pin/unpin, borrar, listar con densidad y "load more".
- **Mantenimiento** (`maintenance.tsx`): `runDoctor`, `runSecurityAudit`, `runBackup`,
  `runCurator` + `getCuratorStatus` + `setCuratorPaused`, `getMemoryStatus` + `resetMemory`,
  `runDebugShare`, y un `getActionStatus` que **polea cada 1,2 s hasta 5 minutos** (240
  iteraciones) para acciones largas, publicando la tarea en `store/activity`.

Ese último detalle es el patrón: una acción de mantenimiento no bloquea la UI; crea una tarea
observable y se pollea el estado.

### 5.4 `/cron`

`blueprints.tsx` (plantillas de trabajo programado), `cron-job-model.ts` (modelo del job),
`job-state.ts` (estado derivado: próximo disparo, última corrida, salud), `cron-actions.ts`,
`open-cron-run.ts` (saltar a la sesión que produjo una corrida). En el chat, el sidebar muestra
una sección de cron jobs (`cron-jobs-section.tsx`), así que el trabajo programado es visible donde
se trabaja, no sólo en su página.

### 5.5 `/profiles`, `/agents`, `/messaging`, `/webhooks`, `/artifacts`, `/session-import`

- **profiles**: crear/renombrar/borrar con diálogos propios; el perfil aísla datos y define el
  gateway al que apunta.
- **agents**: roster (`getAgentRoster` por IPC) con roster-source y estado
  (`roster-source-fetch.ts`, `roster-source-status.ts`), roster de flota (`use-fleet-roster.ts`,
  `fleet-rail.ts`).
- **messaging**: plataformas, allowlist por contacto, QR de Telegram (`telegram-qr-setup.tsx`),
  iconos por plataforma. En el gateway eso son `connectors.*` (11), `bot_relay.*` (7) y
  `channel_directory.py` / `platform_registry.py` (29 KB) / `delivery*.py`.
- **webhooks**: alta y estado de webhooks entrantes.
- **artifacts**: artefactos que producen las sesiones, con utilidades propias
  (`artifact-utils.ts`).
- **session-import**: `session.foreign.list` / `preview` / `import` con
  `ForeignSource = 'claude' | 'codex'`. Es decir: **importa sesiones de Claude Code y Codex CLI**,
  con preview de turnos antes de importar y un handle opaco en vez de una ruta.

### 5.6 `/settings` — manifiesto, no lista

`settings-manifest.ts` + `settings-search.ts` + `use-settings-search.ts` + `subpage-navigation.tsx`
+ `use-setting-deep-link.ts`: los ajustes son un **manifiesto buscable** con subpáginas y deep
links (`?tab=...`), no un archivo gigante. Secciones: about, apariencia (+subpáginas), chat font,
config (con subpáginas), conexiones, endpoints custom, credenciales/env, delegación (+subdir),
gateway, HUD modifier, keybinds, keys, modelos locales (browse/hardware/runtime/quickstart/
descarga), updates gestionados + canal, modelo, notificaciones, onboarding, pet, plugins,
providers, quick entry, screenshot, sesiones, shared metrics, terminal (backend + fuente),
toolset config, uninstall, vault, voz, memoria, billing.

---

## 6. Anatomía del backend

El gateway no es un archivo: es un sistema de archivos-plano con nombres que dicen la
responsabilidad. Casi todo el estado del producto vive acá.

### 6.1 Arranque, turno y apagado

`gateway/run_*.py` es la máquina de estados del proceso:

| Familia | Archivos | Qué resuelve |
|---|---|---|
| arranque | `run_startup.py` (109 KB), `run_bootstrap*.py`, `run_runtime*.py`, `run_config_loaders.py` | levantar config, runtime, plugins, cron, watchers, perfiles |
| turno | `run_turn.py` (137,8 KB), `run_turn_runner.py` (88,3 KB), `run_turn_prepare.py`, `run_turn_persistence.py`, `run_turn_progress.py` (55,1 KB), `run_turn_hygiene.py`, `run_turn_followup_ack.py` | preparar, ejecutar, persistir, progresar y limpiar un turno |
| concurrencia | `run_busy.py` (88,8 KB), `turn_lease.py`, `turn_executor.py`, `session_busy_controls.py`, `session_queued_controls.py` | qué pasa si llega un mensaje mientras corre otro |
| apagado | `run_shutdown.py` (122,2 KB), `shutdown_flush.py`, `shutdown_watchdog.py`, `shutdown_forensics.py`, `quit-finalization` (desktop) | cerrar sin perder turnos ni dejar procesos |
| barrido | `run_housekeeping_disk.py`, `memory_monitor.py`, `scale_to_zero.py`, `run_idle_exit.py`, `agent_cache_pressure.py` | disco, memoria, idle |

Que exista `run_shutdown.py` de 122 KB y `shutdown_watchdog.py` dice mucho: **cerrar bien es una
feature**, no un `sys.exit`.

### 6.2 Sesiones

`gateway/session_*.py` son ~150 módulos. Agrupados por lo que de verdad hacen:

- **Identidad y autoridad**: `session_identity.py`, `session_authority.py` (84 KB),
  `session_authorities.py`, `session_owner*`, `session_route_identity.py`,
  `session_route_recovery.py`, `session_admission.py`, `session_retry_admission.py`.
- **Persistencia**: `session_persistence.py`, `session_db_recovery.py`, `session_transcript.py`
  (36,2 KB), `session_events.py`, `session_envelope.py`, `session_mutations.py`,
  `session_mutation_compress.py`, `session_mutation_model.py`, `session_mutation_policy.py`.
- **Lectura**: `session_history`/`session_listing.py`/`session_live_listing.py`,
  `session_info_reads.py`, `session_tui_reads.py`, `session_api.py`.
- **Control en vivo**: `session_controls.py` (51,4 KB), `session_control_actions.py`,
  `session_group_controls.py`, `session_pending_controls.py`, `session_clarify_*`,
  `session_sudo_prompt.py`, `session_secret_prompt.py`.
- **Ciclo de vida**: `session_lifecycle.py`, `session_resume`, `session_recovery.py`,
  `session_complete_path.py`, `session_prune.py`, `session_stall.py`, `session_finite.py`.
- **Sub-agentes / workers**: `session_worker*.py`, `session_managed_worker*.py`,
  `session_runtime_workers.py`, `session_worker_delegation.py`.
- **Dominio**: `session_cron.py`, `session_kanban.py`, `session_a2a.py`, `session_automation.py`,
  `session_bot.py`, `session_hosted_*` (rooms), `session_goals` (`run_goals.py`),
  `session_refine.py`, `session_onboarding.py`, `session_setting_commands.py`.

### 6.3 Streaming

`stream_consumer.py` (53,3 KB), `stream_consumer_transport.py` (32,9 KB),
`stream_consumer_think.py` (separa thinking del texto), `stream_consumer_fences.py`, 
`stream_consumer_fallback.py`, `stream_dispatch.py`, `stream_events.py`,
`streaming_tts_consumer.py` (TTS en vivo mientras llega el texto).

Del lado del gateway: `tui_gateway/event_publisher.py`, `event_replay.py` (ring + `seq` + epoch),
`agent_callbacks.py` (traduce callbacks del agente a eventos), `agent_factory.py`.

### 6.4 Trabajo de fondo

- **cron**: `cron/` + `session_cron.py` + `cron_store_notices.py` + `cron-trigger-controller.ts`.
- **watchers**: `kanban_watchers*.py` (subscriptions con cursor sobre `task_events`,
  claim de eventos, notificador), `session_automation.py`, `wake.py` + `wake-indicator`.
- **delivery**: `delivery.py`, `delivery_ledger.py` (31,5 KB), `rich_sent_store.py`,
  `run_delivery_queue_watch.py`: entrega confiable a plataformas con libro contable.
- **rooms multi-agente**: `hosted_rooms*.py` (14 módulos, 340 KB) con discusión, driver, peer,
  réplicas, política por checkpoint, work records/lineage. Es el equivalente a una sala compartida
  entre agentes.
- **plugins/hooks**: `plugins/`, `hooks.py`, `builtin_hooks/`, `plugin_command_choice.py`,
  `plugin_keyless_inject.py`, `run_plugin_injection.py`, `run_plugin_rewire.py`.

### 6.5 Multi-máquina

`host_topology.py`, `host_attach.py`, `host_rendezvous.py` (33,9 KB), `pairing.py` (30,8 KB),
`control_socket.py` (28,7 KB), `compute_host.py` + `compute_host_bridge.py` (en tui_gateway),
`bot_relay/`, `hosted_room_peer*.py`. Un backend puede atender a varias superficies y delegar
cómputo a otro host.

---

## 7. Lo adoptable para nexus-v3

Ordenado por **relación valor/riesgo**, no por cuánto brilla. Cada ítem dice qué toca.

### 7.1 Alto valor, bajo riesgo

**1. Contrato de transporte generado desde Python.**
Hoy nexus-v3 tiene 139 rutas REST + SSE escritas a mano en `src/api/server.py` (360 KB) y un
cliente TS que las consume por string. Hermes demuestra el patrón: Python es la fuente, un script
genera `contract.generated.ts`, un test falla si quedó viejo. En nexus-v3 se puede hacer sin
romper nada: generar tipos de request/response de los modelos Pydantic que ya existen y consumirlos
desde `ui/src/lib/api.ts`. Costo: un generador (~150 líneas) + un test de frescura. Ganancia: la
UI deja de adivinar shapes.

**2. Replay de eventos con `seq` + epoch + gap.**
nexus-v3 ya tiene `/api/events` (SSE) y `/api/sessions/{id}/replay`. Falta el contrato: numerar
cada evento por sesión, estampar el epoch del proceso y, cuando el cliente reconecta y el ring se
truncó, emitir un evento de `gap` que fuerza re-sync en vez de dejar la UI con estado parcial.
Barato y elimina una clase entera de bugs de "se quedó colgado el chat".

**3. Importar sesiones de otros agentes.**
`session.foreign.list/preview/import` con `ForeignSource = 'claude' | 'codex'`. En nexus-v3 el
formato de sesión propio ya está en SQLite (`session_store.py`, `session_log.py`); importar
significa parsear JSONL/turnos ajenos a `messages`. Es la feature más barata de copiar y la de
mejor primera impresión para alguien que llega con historial en otra herramienta.

**4. Modo de aprobación `manual | smart | off` por perfil.**
nexus-v3 tiene `approval_policy.py`, `approval_gate.py` y niveles de permiso por herramienta. Lo
que falta es el **modo** como concepto de primera clase, con chip en la barra y sincronización al
backend. Reusa lo que ya existe; es sobre todo UI + un campo de config.

**5. Acción larga = tarea observable, no bloqueo.**
El patrón de `maintenance.tsx`: crear tarea en `store/activity`, pollear `getActionStatus` cada
~1,2 s con techo de 5 min, mostrar progreso y permitir seguir usando la app. nexus-v3 tiene
`doctor.py`, `diagnostics.py`, `dream_distill.py`, `learning_service.py` — todos candidatos a
correr así desde la UI.

### 7.2 Alto valor, riesgo medio

**6. Workbench tileable (zonas + panes keep-alive).**
Es la diferencia estructural más grande entre las dos apps. nexus-v3 hoy cambia de **vista** en una
ventana (14 vistas en `App.tsx`), así que ver el diff y el chat a la vez es imposible. El modelo de
Hermes es concreto y copiable en partes:
- primero un layout fijo de 2 columnas con paneles **keep-alive** (chat + un panel lateral
  conmutable);
- después splits con árbol (un `grid-to-tree` chico) y presets;
- al final floating/parked, que es lo caro.
No hace falta copiar el motor de zonas completo para ganar el 80%.

**7. Modo de interfaz `simple | advanced` como resolver.**
No es un tema: es una función `effective = sessionReveal ?? policy[mode] ?? preference`. Permite
que nexus-v3 arranque simple para uso normal y muestre terminal/grafo/tool payloads a quien los
pide, sin escribir dos UIs ni sobrescribir preferencias.

**8. Preferencias como manifiesto buscable.**
nexus-v3 no tiene una página de ajustes grande todavía; cuando la tenga, el patrón
manifiesto + búsqueda + subpáginas + deep link evita el archivo de 2.000 líneas. Adoptarlo *antes*
de escribirla es más barato que refactorizarla después.

**9. Panel de contexto con desglose.**
`session.context_breakdown` + panel en la barra. nexus-v3 ya calcula presupuesto
(`context_limits.py`, `compaction_budget.py`, `/api/compaction`), así que exponer el desglose
(sistema / historial / tools / archivos) es casi gratis y explica por qué compacta.

**10. Catálogo único de capacidades.**
Una sola página con MCP + tools + skills + plugins, cada uno con estado, instalar/actualizar,
probar y logs. nexus-v3 tiene los datos en `/api/mcp/*`, `/api/skills`, `/api/tools`,
`/api/plugins` pero repartidos en paneles. Unirlos es trabajo de UI, no de backend.

### 7.3 Ideas sueltas que valen

- **`replayed: true`** en eventos recuperados: la UI no debe animar como nuevo lo que es replay.
- **Toast de error con acciones**, como el banner del screenshot: actualizar clave, reintentar,
  cambiar proveedor, abrir logs, enviar diagnóstico, copiar detalles. Seis salidas en vez de un
  "error".
- **`attach` antes de mutar**: las mutaciones de sesión viajan con la revisión, así una ventana
  vieja no pisa el estado de una nueva. nexus-v3 tiene checkpoints; falta el CAS por revisión.
- **Nombres de archivo como documentación**: `restore-pending-approval.ts`,
  `queue-if-busy`, `discard-lost-prompt.ts`, `resume-exhausted-overlay.tsx`. Cada caso raro tiene
  nombre propio y archivo propio; eso es lo que hace mantenible una UI de 600k líneas.
- **Panel de preview con anotación** (`preview-annotate-*`): el navegador embebido donde el humano
  marca y el agente lee la marca.

---

## 8. Lo que NO conviene adoptar

| Cosa | Por qué no |
|---|---|
| Gamificación (pet, HUD de juego, vibe hearts, wake word) | Es engagement, no capacidad. Suma superficie y cero paridad funcional. |
| Facturación / free tier / challenge de cuenta | Modelo comercial de un servicio alojado; nexus-v3 es local y no cobra. |
| Billing y `subscription.*` en el contrato | Idem: acopla el protocolo a un negocio que no tenemos. |
| Multi-tenant real por perfil | Implica migraciones de datos y ruteo por perfil en todo el backend. Para PC1 (un usuario, varias gemas) el costo no se paga. |
| Electron con 648 módulos de main | Copiar el peso, no la lección. Lo valioso es el bridge angosto y el bootstrap negociado, no el árbol de archivos. |
| `hosted_rooms` (340 KB) | Es un producto multi-agente con política, réplicas y linaje. nexus-v3 tiene `team.py`, `dag_coordinator.py` y la colmena; duplicarlo sería peor. |
| Design system con `components/ui` de ~100 primitivas | Ya tenemos 4 primitivas y una paleta fija. El principio ("tokens, una fuente por concepto") sí; el inventario no. |

---

## 9. Paridad: nexus-v3 vs Hermes Desktop

Leyenda: **=** equivalente · **+** nexus-v3 hace más · **−** falta en nexus-v3

| Capacidad | Hermes Desktop | nexus-v3 | Δ |
|---|---|---|---|
| Transporte | JSON-RPC/WS, contrato generado desde Python, 392 métodos | REST 139 rutas + SSE, sin contrato generado | − |
| Streaming de turno | eventos `message.*` / `tool.*` / `subagent.*` con `seq` | SSE con eventos por sesión | = |
| Replay / reconexión | `session.events.since` + epoch + `replay_gap` | `/api/sessions/{id}/replay` sin epoch ni gap explícito | − |
| Aprobaciones | request + `approval.respond`; modos `manual/smart/off` | `/api/approvals` + `/api/questions` + niveles de permiso | = |
| Cola / steer mientras corre | cola por sesión + `prompt.steer_queued` + redirect | `/steer`, `/rewind`, `sequence_guard.py` (sin cola en UI) | − |
| Branch / fork / rewind / árbol | branch desde mensaje, CAS sobre padre | `/branch`, `/fork`, `/rewind`, `/tree`, checkpoints git | **+** |
| Compresión de contexto | `session.compress` | `compaction.py`, `compaction_budget.py`, `/api/compaction` (4 capas) | **+** |
| Desglose de contexto | `context_breakdown` + panel | cálculo interno, sin panel | − |
| Workbench tileable | 35 módulos: zonas, splits, presets, parked, floating | 14 vistas conmutables en una ventana | − |
| Modo de interfaz | `simple`/`advanced` como resolver | no existe | − |
| Memoria / grafo | starmap (d3-force, recencia, timeline, share) | `GraphView` + `/api/graph/*` (11): cite, history, restore, path, related, maintain | **+** |
| RAG | — | `/api/rag/*` (índice, search, stats), `multi_signal_retrieval.py` | **+** |
| Catálogo de capacidades | 1 página: MCP + tools + skills + plugins + catálogo | paneles separados: `skills`, `mcp`, `tools`, `plugins` | − |
| Sub-agentes | `subagent.*` (10), transcript propio, steer/tail | `/api/agents`, `sub_agent_spawner.py`, `SubAgentsPanel`, `dag_coordinator.py` | **+** |
| Cron / programado | página + blueprints + sección en sidebar | `scheduler.py`, `jobs.py`, `task_queue.py` (sin página) | − |
| Importar sesiones ajenas | claude / codex con preview | no existe | − |
| Perfiles aislados | sí (datos + gateway por perfil) | gemas y personas, sin aislamiento de datos | − |
| Multi-máquina | host topology, pairing, rooms, SSH, cloud | `nexus_hive.py` + colmena en VPS (`nexus-sovereign`) | = |
| Update | 24 módulos con handoff, canales, marker, migración de state-db | `self_update.py`, `updater.py`, `/api/updates/auto` | = |
| Voz | `voice.*` (7) + TTS en streaming | `voice_engine.py`, `/api/voice/{tts,stt,status}`, `VoiceMic` | = |
| Navegador embebido | pane preview con anotación, popout, consola | no existe | − |
| Terminal | pane + `terminal-ipc` | `TerminalPanel` + `/api/exec` + sandbox/jail | = |
| Editor de archivos | panes files/review (diff nativo) | `FileEditor`, `ReviewPanel`, `/api/fs/*`, `/api/diff` | = |
| LSP | no | `lsp_client.py` | **+** |
| Automejora / refinamiento | `session_refine.py`, skill learning | `self_learning_loop.py`, `skill_opt.py`, `dream_distill.py`, `learning_graph.py` | **+** |
| Sandbox de ejecución | policies por sesión | `sandbox_service.py`, `command_guard.py`, jail de exec | **+** |
| Tests | pytest + vitest + Playwright e2e | 137 archivos pytest, sin e2e de UI | − |
| Gamificación / billing | extenso | ninguno | n/a |

Resumen honesto: **nexus-v3 gana en profundidad de agente** (memoria, grafo semántico, RAG,
compactación, LSP, sandbox, automejora) y **Hermes gana en producto de escritorio** (workbench,
contrato de transporte, replay, modos, catálogo unificado, operación). La paridad que falta no es
de inteligencia: es de **superficie y de contrato entre capas**.

Orden sugerido para cerrar la brecha, por impacto/esfuerzo:
1. Contrato generado (7.1) — barato, habilita todo lo demás.
2. Replay con `seq`/epoch/gap (7.2).
3. Import de sesiones ajenas (7.3).
4. Panel de contexto + modos de aprobación (7.4/7.5).
5. Layout de 2 paneles keep-alive, no el workbench completo (7.6).
6. Catálogo único de capacidades (7.10).

---

## 10. Evidencia

Todo lo afirmado arriba sale de estos artefactos, verificados en esta máquina:

| Qué | Dónde |
|---|---|
| App corriendo | pid 219992, hwnd 526986, título `Hermes`, 1920x1032, ruta `#/20260906_145139_3d4431` |
| Screenshot | `C:\Users\cjtr\AppData\Local\Temp\opencode\hermes_win.png` |
| Tres autoridades | `apps/desktop/AGENTS.md` |
| Contrato de diseño | `apps/desktop/DESIGN.md` |
| Rutas | `apps/desktop/src/app/routes.ts` (12 rutas, overlay vs workspace) |
| Contrato RPC | `apps/shared/src/gateway-contract.generated.ts` (7.270 líneas, 392 métodos, 24 grupos) |
| Protocolo y timeouts | `apps/desktop/src/api/client.ts`, `canonical-protocol.ts`, `apps/shared/src/gateway-events.ts` |
| Bridge Electron | `apps/desktop/electron/preload.ts` (`window.hermesDesktop`) |
| Bootstrap | `apps/desktop/electron/bootstrap-*.ts`, `backend-*.ts`, `local-gateway*.ts` |
| Workbench | `apps/desktop/src/components/pane-shell/` (35 archivos) |
| Modo de interfaz | `apps/desktop/src/store/interface-mode.ts` |
| Modo de aprobación | `apps/desktop/src/app/shell/approval-mode-menu.tsx`, `src/store/approval-mode.ts` |
| Starmap | `apps/desktop/src/app/starmap/{simulation,render,constants,types}.ts` |
| Mantenimiento | `apps/desktop/src/app/command-center/maintenance.tsx` |
| Import ajeno | `ForeignSource = 'claude' \| 'codex'` en el contrato; `session.foreign.*` |
| Backend | `gateway/` (326 py, 109.794 líneas), `tui_gateway/` (118 py, 38.662), `gateway/run_turn.py` (137,8 KB), `gateway/run_shutdown.py` (122,2 KB) |
| Escala del escritorio | `apps/desktop/src` 3.192 archivos / 597.255 líneas; `electron` 648 / 118.078 |

No se modificó nada del árbol de Hermes: el análisis es de sólo lectura sobre el fuente instalado.

