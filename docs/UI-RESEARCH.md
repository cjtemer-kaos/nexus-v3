# Investigación: cómo construir una UI decente para nexus-v3

> Fecha: 2026-09-30 · Autor: opencode · Estado: investigación completa (fase de diseño, sin implementación)
>
> Objetivo: definir cómo debe ser la UI de nexus-v3 — **simple y profesional**, al estilo de
> opencode, Hermes, Claude Code y las herramientas más usadas del mercado — tras el
> diagnóstico de la UI actual (React + Vite + Tailwind en `ui/`).

---

## 1. Diagnóstico de la UI actual (evidencia)

Capturas reales del UI corriendo sobre backend vivo `:8765` (2026-09-30):

| Captura | Contenido |
|---|---|
| `.playwright-mcp/nexus-v3-full.png` | Pantalla completa (1440×900), estado vacío |
| `.playwright-mcp/nexus-v3-msg.png` | Chat con mensaje en vivo + "Trajectory: 2 pasos" |
| `.playwright-mcp/nexus-v3-sesion2.png` | Cambio de sesión (bug: historial no recarga) |
| `.playwright-mcp/nexus-v3-actual.png` | Vista inicial |

### 1.1 Lo que está mal (por orden de gravedad)

1. **Fuga de internales a la UI** — el usuario ve arquitectura, no producto:
   - Pill verde en el topbar: `WAL activado · FTS5 · sesión e2e-chat`.
   - Sidebar: `SESIONES · WAL+FTS`, sesión `PONG / default · e2e-chat · FTS`, hashes tipo `759ea82f84d6`.
   - Card inferior: `Cerebro local / Enlazado via NEXUS_V3_BRAIN → ./brain` + pills `FTS5` / `RAG 768d`.
   - Footer: `Bucle Agente TDAO · Guardia de Alcance · Compactación 4 capas` + línea de estado
     `config.yaml · nada hardcodeado · distribuible` + `Alcance: raíces permitidas ./workspace, ./data · denegado **/env` + `nexus-v3 3.0.0-alpha`.
   - Nav items con meta cruda: `Chat [Bucle Agente TDAO]`, `Grafo [Conocimiento]`, `Editor [Alcance FS]`, `Terminal [Protegido]`.
   - **Regla que falta**: en la UI solo se comunica *estado de usuario* (conectado, listo, ocupado,
     error). Los internales (WAL, FTS5, RAG, hashes, rutas, versiones alfa) van a un panel
     "Diagnóstico" colapsado o a logs — nunca en chrome permanente.

2. **Ruido de pills/badges** — casi cada elemento tiene un chip decorativo (ALFA, compactación,
   Alcance, FTS…). Efecto: nada resalta, sensación de dashboard genérico, no de herramienta.

3. **Bugs funcionales detectados en la sesión de prueba**:
   - **Cambio de sesión no carga el historial**: clic en sesión `hola mundo` → el breadcrumb
     cambia a `sesión 759ea82f84d6` pero el área de chat sigue mostrando los mensajes de la
     sesión PONG. (Verificado con Playwright, captura `nexus-v3-sesion2.png`.)
   - Transitorio: `ERR_CONNECTION_REFUSED` a `127.0.0.1:8765` cuando el backend se cae —
     la UI no muestra estado de desconexión (necesita banner de reconnect/intento).
   - El título de sesión se muestra como crudo de contenido (`` ```python # hola mundo ``` ``,
     `[presupuesto agotado antes de…`) — hace falta *display title* saneado + primer mensaje como fallback.

4. **Burbujas de chat pesadas**: padding enorme, esquinas redondeadas excesivas, mensajes
   flotando en un vacío sin densidad ni controles por mensaje (copiar/regenerar/stop no existen).

5. **Estado vacío con jerga**: "Arnés TDAO conectado al backend vivo (8765)… usa `todo_write`,
   `web_fetch` y Guardia de permisos". Un welcome debe invitar a actuar, no describir la arquitectura.

6. **Layout con/sidebar demasiado ancho (~420px)** y con scroll interno fragmentado
   (sesiones se cortan a media altura con un gutter extraño).

### 1.2 Lo que está bien (reutilizar)

- Tema oscuro consistente violeta/zinc con acento `#8b5cf6` — base sólida, solo necesita contención.
- **`Trajectory: N pasos` colapsable** — buen germen del patrón "progressive disclosure"
  (la idea es correcta; la ejecución, genérica).
- Modelo selector en topbar (Auto/router) con hint — correcto, solo hay que ocultar `infer_hint`.
- Cmd/Ctrl+K buscador y `/` comandos ya presentes — el atajo está, falta pulir.
- Streaming y tarjetas de tool call ya funcionan end-to-end (verificado en vivo: envío → `PONG`
  → trajectory de 2 pasos).

---

## 2. Análisis de las herramientas de referencia

### 2.1 opencode (TUI, TypeScript) — "simple" por máxima

- **Stack**: TypeScript + `@opentui/solid` = OpenTUI (core **Zig** + reconciler **SolidJS**);
  shell propio. (Los artículos que dicen "Go + Bubble Tea" están desactualizados.)
- **Código**: `packages/opencode/src/cli/cmd/tui/app.tsx`; componentes en `packages/tui`
  (prompt, dialog-command, dialog-session-list, dialog-agent, dialog-mcp, dialog-status,
  dialog-theme-list, dialog-keybinds).
- **Lección**: *una sola superficie*. No hay dashboard ni paneles: prompt abajo, historial
  arriba, diálogos modales superpuestos para todo lo secundario (sesiones, agentes, temas,
  keybinds). El usuario vive en un flujo; el resto aparece solo bajo demanda.
- **Ficha**: command palette como centro de gravedad (⌘K), sesión lista como overlay, tema
  seleccionable, status bar mínima (modelo · contexto).

### 2.2 Claude Code (React + Ink fork) — el gold standard de densidad

- **Stack**: React 19 ConcurrentRoot + Ink *forked*; motor propio: Yoga (flexbox), pantalla
  empaquetada en `Int32Array` double-buffered, **diff a nivel celda**, ANSI optimizer,
  alternate screen, virtual scrolling. 144 componentes, objetivo 60fps con streaming.
- **Estructura** (claude-code-from-source.com/ch13):
  `<REPL><AlternateScreen><FullscreenLayout><ScrollBox sticky><VirtualMessageList>`
  + `<StatusLine>` (modelo │ permisos │ cwd │ tokens │ costo)
  + `<PromptInput>` (autocomplete + footer pills de atajos)
  + ~15 overlays: PermissionRequest, ModelPicker, ThemePicker, GlobalSearchDialog…
- **Lecciones**:
  1. **Status line = una fila** con lo esencial (modelo, modo de permisos, contexto/costo).
     Nada más. Esto reemplaza *todo* el chrome actual de nexus-v3.
  2. **Virtuellist + sticky scroll** para que el historial nunca se cuaje.
  3. **Todo lo demás es overlay**: elegir modelo, tema, buscar, pedir permiso — modales
     contextuales, no widgets permanentes en pantalla.
  4. Controles por mensaje (copiar, reintentar) colgados del mensaje, no de un panel.

### 2.3 Codex CLI (Rust, ratatui) — arquitectura de protocolo

- Fork de ratatui+crossterm (368 archivos / 221k LOC, 575 tests snapshot).
- **Inline viewport** con scrollback nativo (DECSTBM): no usa alt-screen como pantalla
  principal — el chat se mezcla con la salida de comandos tipo terminal.
- **Seam JSON-RPC** (`turn/start|steer|interrupt`) contra `app-server`: el TUI es solo un
  cliente de la misma API pública que usaría cualquier otro frontend.
- **Lección para nexus-v3**: ya tenemos el backend (:8765) — el frontend debe ser un cliente
  puro de una API de eventos tipados (SSE), sin lógica de negocio en React. Eventos
  `turn_started / tool_call / delta / turn_completed / error` tipados = UI predecible y
  reemplazable (mañana una TUI o un Electron encima de la misma API).

### 2.4 Grok Build (Rust, ratatui) — confirmación de tendencia

- Otro TUI profesional sobre ratatui. Las herramientas de agentes líderes convergen en:
  TUI para dev power-users, o chat web denso con overlays. Ninguno hace "dashboard SaaS".

### 2.5 goose (Block) — equivalente web de referencia

- **Stack**: Electron Forge + Vite + React + Tailwind + HashRouter; habla con el CLI goose
  por **ACP (WebSocket)**; ventana 940×800 con `windowStateKeeper`, vibrancy en macOS.
- **Vistas**: Hub (onboarding), Pair (chat), Sessions, Settings, Extensions, Recipes,
  Schedules, Skills. Chat: `ChatView`, `ChatInput`, `ProgressiveMessageList`,
  `GooseMessage`, `ToolCallWithResponse`, `MarkdownContent`. Session bar: `DirSwitcher`,
  cost tracker, mode selection.
- **Lecciones**:
  - **Modos de sesión explícitos** (modelo, modo, directorio de trabajo) en una barra compacta —
    equivalente web de la StatusLine de Claude Code.
  - **Lista de sesiones progresiva** (no solo título: estado y resumen).
  - **ToolCallWithResponse** como componente propio: nombre legible + args colapsados +
    resultado colapsado + estado (running/ok/error).
  - ACP como protocolo de transporte: útil si algún día exponemos el arnés a otros frontends.

### 2.6 Hermes (referencia local del usuario)

Dos proyectos:
- `D:\ias\proyectos\hermes-agent` (Python): `web/` Vite+React, `ui-tui/`, `tui_gateway/`,
  `hermes_cli/` dashboard :9119.
- `D:\ias\autopsia\hermes-workspace` v2.3.0 (Electron + React + TanStack Router/Start +
  Tailwind 4 + framer-motion + three.js + Monaco + `scifi-theme.css`).

Captura de `screenshots/scifi-theme.png`: tema navy/cyan **HUD sci-fi cohesivo** — sidebar
con secciones (MAIN/KNOWLEDGE/SESSIONS), stat cards, gráficos de uso, logros, status bar
inferior con CPU/RAM/disk. Es *denso* pero **todo habla el mismo idioma visual**
(mono uppercase, bordes finos, un solo acento, lenguaje de instrumento).
- **Lección**: la densidad no es el problema — **la inconsistencia y la jerga sí**. Hermes se
  siente "pro" porque el estilo es unitario y cada dato tiene un lugar fijo. nexus-v3 puede
  ser denso (estado del arnés es información valiosa) si se **agrupa detrás de secciones
  con etiquetas de usuario y un solo dialecto visual**.

---

## 3. Patrones universales (qué hace "decente" una UI de agente en 2026)

Fuentes: uipotion.com AI Response Rendering · kagan.ai Agent UX Patterns Catalog ·
ai-tldr.dev chatbot UX patterns · metacto production AI chat · 72Technologies streaming
tool calls · jasonlaster.com 15 chat UI best practices · CopilotKit generative UI.

### 3.1 Layout canónico (todos convergen)

```
┌──────────────┬──────────────────────────────────────────────┐
│  Sesiones    │  [breadcrumbs]      [modelo ▾] [⌘K] [⚙]      │  ← barra: 1 fila, sin más
│  (lista      ├──────────────────────────────────────────────┤
│   compacta,  │                                              │
│   progresiva)│        Historial (virtual list)              │
│              │        └ burbujas con bloques tipados        │
│   [+ nueva]  │                                              │
│              ├──────────────────────────────────────────────┤
│  [modo/dir]  │  [📎] ⌘ escribe… (placeholder dinámico) [▶/■] │  ← input anclado
│              ├──────────────────────────────────────────────┤
│              │  modelo · modo permisos · ctx · costo        │  ← STATUS LINE (1 fila)
└──────────────┴──────────────────────────────────────────────┘
```

- **Chat-first**: el chat ocupa >80% del viewport. Las demás vistas (Grafo, Editor,
  Terminal) son *modos* del mismo shell, no dashboards paralelos.
- **Sin chrome permanente de diagnóstico**: la status line de 1 fila reemplaza pills,
  footer de jerga, cards de "Cerebro local", etc.

### 3.2 Mensaje = lista de bloques tipados (no `content: string`)

```ts
type Part = TextPart | ReasoningPart | ToolPart | SourcePart | ErrorPart
```

- Modelo canónico `UIMessage` (Vercel AI SDK) / `MessagePart` (TanStack) / eventos
  `TEXT_MESSAGE_CONTENT · TOOL_CALL_START · TOOL_CALL_ARGS · TOOL_CALL_END ·
  THINKING_START · RUN_FINISHED` (TanStack SSE).
- **Transcript + tail**: la lista se parte en "comprometida" (memoizada, no re-renderiza)
  y "activa" (la única que repinta por token). Codex lo llama `transcript + active_cell`;
  React lo resuelve con `memo` + cola de ≤20Hz (`experimental_throttle: 50`).

### 3.3 Tool calls: tarjetas con ciclo de vida (3 fases)

1. **Announce**: al conocerse el nombre → tarjeta con **intención legible** ("Buscando tus
   pedidos recientes…"), no JSON ni `search_orders_raw`.
2. **Run**: indicador + estado (`calling / succeeded / failed`) — 3 estados, no más.
3. **Result**: resultado colapsado + disclosure para args/resultado crudo; error dentro de
   la tarjeta, el resto del turno sigue.

- **Ocultar tool calls es el fallo nº1 de producción** ("the agent or a magic trick").
- Argumentos: parsear solo al cerrar el bloque (`content_block_stop`), nunca por delta.
- **Nunca streamear afirmaciones sobre un resultado antes de que la tool retorne**.

### 3.4 Progressive disclosure (el patrón dominante)

- **Resumen siempre** (la respuesta final es el contenido primario) →
  **razonamiento bajo demanda** (un clic: "Pensó 4s" expandible) →
  **traza completa** para power users (trajectory, args, outputs).
- nexus-v3 ya tiene el germen: `Trajectory: N pasos` → evolucionar a
  `Pensó 3s · usó 2 herramientas ▸` con detalle expandible.

### 3.5 Ciclo de vida de la respuesta (estados visibles)

`idle → queued → streaming → tool-running → awaiting-review → complete | cancelled | errored`

- **TTFT < 300–700ms** percibido rápido: typing dots + label de estado ("Pensando…",
  "Leyendo config.yaml…") desde el envío.
- **Stop** reemplaza al botón Enviar en la misma posición; `Esc` global; sin diálogo
  de confirmación; el parcial queda visible marcado "interrumpido" + reintento.
- **Errores**: 6 modos diferenciados (pre-stream, mid-stream, rate limit, content-filter,
  tool error, evento de error en stream). Tras `HTTP 200`, el error *solo* puede llegar
  como evento en el stream — renderizarlo inline con retry, conservando el parcial.
- Cierre de pestaña → el backend debe abortar el turno (no facturar tokens huérfanos).

### 3.6 Controles por mensaje (una vez completado)

copiar · regenerar · editar prompt · (stop mientras vive) · feedback — en hover, anclados
al mensaje.

### 3.7 Errores y vacíos

- Estado vacío: 1 headline de acción ("¿Qué hacemos hoy?"), 3-4 sugerencias concretas con
  atajos, sin párrafos de arquitectura.
- Cada error: clasificación + mensaje humano + acción de recuperación (retry con el prompt
  preservado / nueva sesión / rephrase).

### 3.8 Accesibilidad (mínimo viable)

- Una sola región `aria-live="polite"` por conversación (transiciones de estado, no tokens).
- `role="status"` en el typing indicator; foco visible; contraste AA.

---

## 4. Recomendación para nexus-v3

### 4.1 Stack (mantener + 2 piezas)

**Mantener**: React 19 + Vite + Tailwind 4 + zustand + Radix (ya instalado, maduro, el
problema no es el stack sino el diseño). Monaco y react-force-graph quedan para Editor/Grafo.

**Agregar (justificado)**:
1. **`virtual` (@tanstack/react-virtual)** — lista de mensajes virtualizada (patrón
   VirtualMessageList de Claude Code). Histories largas hoy crecen sin límite.
2. **`streamdown` o `react-markdown` en modo streaming** — Markdown incremental tolerante a
   fences sin cerrar; `skipHtml` en la ruta de chat (salida del modelo = texto, no HTML).

**No agregar**: nada de three.js/framer-motion pesado en el shell — la "sensación pro" viene
de densidad tipográfica y micro-estados, no de animación.

**Protocolo**: definir eventos SSE tipados en el backend (`turn_started`, `delta`,
`tool_started`, `tool_finished`, `review_required`, `turn_finished`, `error`) estilo seam
JSON-RPC de Codex — el frontend es cliente puro, testeable y reemplazable.

### 4.2 Principios de diseño (contrato)

1. **Una sola fila de estado** — todo el chrome actual se condensa en status line
   `modelo · modo · ctx% · costo` (Claude Code) + barra superior de 1 fila (breadcrumb,
   modelo, ⌘K). Pill de WAL/FTS5: **eliminado**.
2. **Jerga solo bajo demanda** — panel "Diagnóstico" (drawer) para WAL/FTS5/RAG/brain/paths.
   Nada de eso en sidebar, footer ni topbar.
3. **Chat-first** — el chat es la pantalla; Grafo/Editor/Terminal son modos, no homes.
4. **Bloques tipados + progressive disclosure** — respuesta limpia arriba; razonamiento y
   trajectory colapsados; tool cards con 3 fases.
5. **Keyboard-first** — ⌘K paleta, `/` comandos, `@` archivos, `Esc` stop, `⌘↵`/Enter enviar.
6. **Un dialecto visual** — un acento (violeta), tipografía mono solo para código/IDs,
   radios y sombras consistentes; **máximo 2 pills por pantalla visibles**.
7. **Densidad controlada estilo Hermes** — información abundante pero agrupada y con
   etiquetas de usuario ("Conocimiento", no "WAL+FTS").

### 4.3 Wireframe del shell propuesto

```
┌───────────────────────────────┬──────────────────────────────────────────────┐
│ N³ Nexus v3            [ALFA] │ ⌘ Sesiones / Chat              [Auto ▾][⌘K] │
│ [+ Nueva conversación]        ├──────────────────────────────────────────────┤
│ ────────────────────────────  │                                              │
│ HOY                           │   tú: hola, responde solo PONG        18:39  │
│  ● PONG               18:39   │   ─────────────────────────────────────      │
│  ● hola mundo         17:02   │   PONG                                  18:39 │
│  ● chat-ok            16:40   │   ▸ Pensó 1s · 2 herramientas                │
│ ────────────────────────────  │      └ Trajectory: 2 pasos (expandible)     │
│ SESIONES                      │                                              │
│  ... (título saneado)         │                                              │
│ ────────────────────────────  ├──────────────────────────────────────────────┤
│ 🧠 Cerebro ● conectado        │ [📎] Mensaje… / comandos  @archivos   [Enviar]│
│ ⚙ Ajustes   ⚙ Diagnóstico    │ auto · permisos guía · 12% ctx · $0.00       │
└───────────────────────────────┴──────────────────────────────────────────────┘
```

Cambios vs. actual: sidebar estrecho (~260-300px) sin pills; card de cerebro → indicador
de 1 línea; Diagnóstico como drawer; footer de jerga → status line de datos de usuario;
bottom-left pasa a ser el equivalente web de la StatusLine.

---

## 5. Plan de rediseño por fases

| Fase | Alcance | Esfuerzo |
|---|---|---|
| **U1 — Limpieza inmediata** (1 sesión) | Eliminar pills/footer/jerga (WAL, FTS5, hashes, rutas, `infer_hint`, "nada hardcodeado"); estado vacío nuevo con 3 sugerencias; título de sesión saneado; sidebar estrecho; status line de 1 fila | bajo — puro CSS/JSX |
| **U2 — Bugs** (1 sesión) | Fix cambio de sesión no carga historial; banner de reconexión cuando :8765 cae; streaming states (typing dots + stop) | bajo-medio |
| **U3 — Mensajes v2** (1-2 sesiones) | Bloques tipados (`Part[]`), markdown streaming con fence tolerance, tool cards 3 fases, trajectory mejorado (`Pensó 1s · 2 herramientas ▸`), controles por mensaje (copiar/regenerar/stop) | medio — toca backend SSE + ChatPanel |
| **U4 — Overlays** (1 sesión) | ⌘K paleta de comandos completa, ModelPicker/ThemePicker como modales Radix, panel Diagnóstico drawer | medio |
| **U5 — Perf** (media sesión) | Lista virtualizada + memo transcript/tail (≤20Hz) | medio |

**Criterio de éxito**: un usuario nuevo abre nexus-v3 y entiende *qué hacer* en <5s;
cero palabras del backend en el chrome; sesión de 50 mensajes sin jank; cada acción del
agente visible como tarjeta con estado.

---

## 6. Fuentes

- **Estado actual**: capturas `.playwright-mcp/nexus-v3-*.png` (backend :8765 en vivo,
  2026-09-30); consola `.playwright-mcp/console-*.log`.
- **opencode**: github.com/sst/opencode → `packages/opencode/src/cli/cmd/tui/app.tsx`,
  `packages/tui/*`.
- **Claude Code**: claude-code-from-source.com/ch13-terminal-ui (REPL, StatusLine,
  VirtualMessageList, overlays, motor Ink forked).
- **Codex/Grok**: github.com/Luolc/locode-core → `docs/research/tui-harness-study.md`
  (ratatui, inline viewport, seam JSON-RPC, snapshot tests).
- **goose**: github.com/block/goose → `ui/desktop/` (Electron+Vite+React, ACP, ChatView,
  ToolCallWithResponse).
- **Hermes**: `D:\ias\autopsia\hermes-workspace` (+`screenshots/scifi-theme.png`),
  `D:\ias\proyectos\hermes-agent` (`web/`, `ui-tui/`).
- **Patrones UX 2026**: uipotion.com/potions/patterns/ai-response-rendering ·
  kagan.ai/catalog/vol-13 · ai-tldr.dev/learn/building-ai-apps/ai-ux-patterns ·
  metacto.com/blogs/ai-chat-ux-patterns-production · 72technologies.com/blog/streaming-tool-calls-agent-ux ·
  jasonlaster.com/posts/2026-04-25-chat-ui · copilotkit.ai/blog/generative-ui-explained.
