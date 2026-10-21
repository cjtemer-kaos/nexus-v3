# Autopsia profunda: `cline/cline` → mecanismos portables a nexus-v3

- **Repo:** `github.com/cline/cline`
- **Commit:** `e9dafbb` (clon shallow, 2026-10-21)
- **Licencia:** Apache-2.0 (los ports son reimplementaciones, no copia literal)
- **Runtime analizado:** `sdk/packages/*` (el runtime nuevo). `apps/vscode/src` es legacy.
- **Método:** lectura de código y de la suite de tests del propio repo. No se ejecutó su
  build (requiere Bun 1.4.2 + Node >= 22); los algoritmos se validaron reimplementándolos
  en Python y corriendo la suite de nexus-v3.
- **Alcance:** contexto/compactación, loop del agente, tools de edición y shell, hooks,
  checkpoints y modelo de sesión. Se excluyen deliberadamente: rotación de credenciales,
  la fuente de Claude Code y el Harness² nativo de Windows (temas parkeados).

---

## 0. Resumen ejecutivo

Cline no gana por el prompt ni por el modelo: gana por **disciplina de contexto**. Su tesis
implícita es que un agente falla menos si el transcript que manda al provider es:

1. **determinista** (misma entrada ⇒ misma proyección),
2. **estructuralmente válido** (nunca un `tool_use` sin su `tool_result`),
3. **acotado por presupuesto real** (no por “los últimos N mensajes”),
4. **auditable** (cada recorte deja una acción con motivo),
5. **recuperable** (overflow y max-tokens tienen un camino de vuelta, una sola vez).

Eso es exactamente lo que a nexus-v3 le faltaba. La tabla siguiente prioriza por
**impacto en fallos reales**, no por novedad:

| # | Mecanismo | Fuente (Cline) | Síntoma que corrige en nexus-v3 | Estado |
|---|---|---|---|---|
| 1 | Proyección de presupuesto con *tool-pair closure*, live tail y `actions` | `extensions/context/budget-projection/project.ts:479` | Cortes que orphanan un `tool_result`, o que borran el pedido actual | **Portado** |
| 2 | Trigger 90% + corrección por subestimación (`actual/estimado`, cap 4) | `extensions/context/compaction.ts:342-362` | Overflow tardío con tokenizers densos (CJK, minificado) | **Portado** |
| 3 | Overflow recovery una vez por run + validación “estrictamente menor” | `agents/src/agent-runtime.ts:1363-1425`, `:2276-2285` | Reintentar con el mismo tamaño y fallar de nuevo | **Portado** |
| 4 | Compactación sin LLM que preserva prompts tipados y respuestas finales | `extensions/context/basic-compaction.ts:452` | `_summary_fallback` conserva solo la cola y pierde la tarea | **Portado (parcial)** |
| 5 | Invariante de corte: nunca entre `tool_use`/`tool_result` ni antes del último prompt tipado | `extensions/context/compaction-shared.ts:368` | El resumen se “come” el pedido del usuario | **Portado** |
| 6 | `apply_patch` difuso con Add/Delete/Move, EOL preservado y atomicidad | `extensions/tools/executors/apply-patch-parser.ts:85` | Parches que fallan por espacios finales o no soportan mover/crear | **Portado** |
| 7 | `command-guard` para plan mode (shell read-only real) | `extensions/tools/command-guard.ts:479` | Plan mode bloquea `exec` entero o lo deja mutar | **Portado** |
| 8 | Cap duro de salida de tools (head+tail con aviso accionable) | `extensions/tools/executors/output-limits.ts:18` | Outputs gigantes reenviados en cada request (costo cuadrático) | **Portado (exec)** |
| 9 | Grupos **adyacentes** de tools paralelas; una secuencial es barrera | `agents/src/agent-runtime.ts:2343-2372` | Ejecutar en paralelo algo que dependía del resultado anterior | Ya corregido en nexus |
| 10 | Hooks por archivo con control JSON por stdout y timeout duro | `hooks/subprocess.ts:131`, `hooks/hook-file-hooks.ts` | Hooks solo in-process, sin extensibilidad por archivo | **Portado** |
| 11 | Checkpoints git con transacción de restore (untracked incluidos) | `session/checkpoint-restore.ts:50` | Snapshots por archivo sin restore multi-archivo consistente | **Portado (repo sombra)** |
| 12 | Compactación agentic con presupuesto propio del summarizer | `extensions/context/agentic-compaction.ts:31` | El request del resumen también puede overflowear | **Portado** |

**Conclusión:** el lote de mayor ROI es 1–5 y 7, y es el que se portó. 6 también se portó
porque el `apply_patch` de nexus era frágil de una forma que ningún prompt arregla.
10–12 son mejoras estructurales grandes; tienen diseño listo pero no código en este lote.

---

## 1. Mapa del repo y evolución

```
sdk/packages/
  shared/   contratos de mensajes/tools/storage (MessageWithMetadata, createMessage)
  llms/     providers, ModelInfo, token accounting, tool schemas
  agents/   AgentRuntime: loop, retries, overflow, ejecución de tools
  core/     extensions: context (compactación), tools, hooks, mcp, plugins
            session: checkpoints, compaction state, message-builder, fork/rewind
  ui/       componentes React
  sdk/      fachada pública
apps/
  vscode/   extensión legacy (prompts/responses.ts, task loop viejo)
  cli/      CLI
  cline-hub/
```

Dos observaciones que importan para no portar lo equivocado:

1. **`apps/vscode/src` ya no es la fuente de verdad.** El loop moderno vive en
   `agents/src/agent-runtime.ts`; la compactación en `core/src/extensions/context/*`.
   Muchos artículos y forks siguen citando el loop legacy.
2. **El core es agnóstico del host.** `AgentRuntime` recibe `prepareTurn`, provider,
   tools y hooks por configuración. Eso es lo que hace sus mecanismos portables: no
   dependen de VS Code.

### 1.1 Contrato de mensajes

`MessageWithMetadata` (en `shared`) tiene, además de `role`/`content`:

- `id` estable (los resúmenes se resuelven por id contra el transcript original),
- `metadata.userRunSpan`: 0 = mensaje sintético o inyectado; ≠ 0 = turno tipado por el
  usuario. Es la señal que distingue “el usuario habló” de “el runtime inyectó algo”.
- `metadata.displayRole`: permite que un mensaje sea `user` para el modelo pero se
  muestre como `system` en la UI (resúmenes de compactación y contexto de hooks).
- `metadata.kind` (p. ej. `compaction_summary`, `synthetic`).
- `metadata.modelToolActivities`: tools ejecutadas por el provider fuera del runtime
  (side effects ya ocurridos; no se pueden replayar).

`content` es una lista de bloques (`text`, `tool_use`, `tool_result`, `thinking`,
`image`, …), no un string. Todo el código de compactación razona sobre bloques.

**nexus-v3** usa dicts OpenAI-style: `role`, `content` (str), `tool_calls[].function.arguments`
(dict), `tool_call_id`, `images`. El port introduce adaptadores que leen ambas formas
(`_content_blocks`, `collect_tool_ids`, `_text_of`), sin migrar el modelo de datos.

---

## 2. `AgentRuntime`: el loop

Archivo: `sdk/packages/agents/src/agent-runtime.ts`.

### 2.1 Estado y ciclo

El runtime mantiene, entre otras cosas:

- `state.iteration` (se incrementa al empezar cada turno; `:842`),
- `state.messages` (transcript canónico),
- `state.lastError` / `lastErrorClass` (se resetean por turno: `:851`),
- `overflowRecoveryAttempted` (`:594`) y `maxTokensRecoveryAttempted`,
- `pendingHookContexts` (bloques de hooks que se inyectan como un solo mensaje),
- `abortController` (aborto cooperativo; se limpia en `finally`, `:1090`).

Ciclo por turno (`:840-1049`):

```
loop:
  iteration += 1
  emit turn-started
  resetLastError()
  {message, finishReason, interrupted} = generateAssistantMessageWithProviderRetry()
  si abortado y sin contenido: emit turn-finished, continue
  si finishReason == aborted: throw normalizeAbortError()
  si content vacío: throw con atribución (nada de assistant vacío)
  persistir/emitir turno
  si hay tool calls:
      flushPendingHookContexts()   // antes de ejecutar, no después
      executeToolCalls()           // grupos adyacentes
  si no hay tool calls:
      evaluar finishReason y nudges
```

Puntos finos:

- **Contexto de hooks**: `flushPendingHookContexts` (`:1099-1112`) junta los bloques y
  los manda como **un** mensaje `user` con `userRunSpan: 0` y `displayRole: "system"`.
  Se inserta **antes** de ejecutar un `tool_call` final sin resolver, para que el modelo
  lo vea en el mismo request que decide la tool. Si se insertara después, se perdería.
- **Assistant vacío**: es un error de atribución, no un turno válido. Evita loops de
  “el modelo no dijo nada, reintentemos” sin diagnóstico.
- **Un turno con tool calls no se reintenta**: los side effects ya ocurrieron. Esto
  aparece en tres lugares (`isRecoverableOverflowTurn`, `isRecoverableMaxTokensTurn`,
  retry policy).

### 2.2 Reintentos de provider

`generateAssistantMessageWithProviderRetry` (`:1202-1279`) solo reintenta si:

- no hubo contenido emitido (o el contenido es descartable),
- no hubo side effects,
- el error está clasificado como retryable (rate limit, 5xx, red, timeout),
- no se agotó el presupuesto de intentos.

La clasificación de errores es explícita (`errorClass`): `context_window_exceeded`,
`rate_limit`, `auth`, `network`, `server`, `unknown`. El loop decide con la clase, no con
el string del mensaje.

### 2.3 Max-tokens recovery

`isRecoverableMaxTokensTurn` (`:1427-1447`):

```
finishReason == "max-tokens"
&& !maxTokensRecoveryAttempted
&& prepareTurn existe
&& el turno NO produjo tool calls
&& !hasModelToolActivity(message)
```

El último chequeo es sutil: si el provider ejecutó tools por su cuenta (metadata), replayar
el turno repetiría side effects. Cuando aplica, `retryTruncatedTurnWithCompaction`
(`:1481`) fuerza **una** compactación y reintenta. Si la compactación no cambió nada o el
reintento vuelve a truncarse, el turno original se persiste y el loop aplica sus nudges
(hasta 3) para que el modelo continúe desde el texto parcial.

La razón de negocio: en servidores OpenAI-compatibles locales (llama.cpp, Ollama, LM Studio)
el output suele estar limitado por el contexto restante, no por `max_tokens`. Compactar
libera espacio y rescata el turno.

### 2.4 Overflow recovery

`generateAssistantMessageWithOverflowRecovery` (`:1363-1408`):

```
first = generateAssistantMessage()
if max-tokens recuperable: return retryTruncatedTurnWithCompaction(first)
if no es overflow recuperable: return first
overflowRecoveryAttempted = true
if no hay prepareTurn: throw ContextWindowOverflowError(no recovery)
emit status-notice (context_overflow_recovery, started)
retry = generateAssistantMessage({overflowRecovery: true})
if retry error && errorClass == context_window_exceeded:
    throw ContextWindowOverflowError(recovery failed)
return retry
```

Y el guard clave en `prepareTurnForModelRequest` (`:2263-2286`):

```
if overflowRecovery:
    shrunk = result.messages !== undefined
             && JSON.stringify(result.messages).length
                < JSON.stringify(request.messages).length
    if !shrunk: throw ContextWindowOverflowError(nothing to compact)
```

Es decir: **no se reintenta un overflow con un request igual o más grande**. La métrica es
longitud serializada (proxy lineal de tokens, suficiente para “¿se removió algo?”), con un
TODO explícito para que `prepareTurn` reporte los tokens reales.

**nexus-v3 hoy:** `reactive_compact` compacta y reintenta, pero no verifica que el request
haya encogido. Si la compactación no puede recortar (p. ej. todo protegido), se reintenta
igual y se pierde el turno. Port pendiente, chico.

### 2.5 Ejecución de tools: adyacencia, no “paralelo global”

`executeToolCalls` (`:2343+`) agrupa **solo tool calls adyacentes** en el mismo assistant
message. Una tool secuencial (o un mensaje entre medio) es **barrera**:

```
for each assistant message:
    group = consecutive tool calls safe to parallelize
    run group concurrently
    next group waits
```

Esto evita el clásico “ejecuté `write_file` y `read_file` en paralelo y leí el archivo
viejo”. En nexus-v3 ya se corrigió con prefijo contiguo; la diferencia es que Cline además
respeta `executionMode` por tool (p. ej. una tool puede declararse no paralelizable aunque
esté adyacente).

---

## 3. Compactación: el corazón del sistema

Archivos:

```
core/src/extensions/context/
  compaction-shared.ts        constantes, tipos de turno, findCutIndex, serialización
  compaction.ts               orquestador: trigger, presupuesto, overflow, estrategias
  basic-compaction.ts         compactación determinista sin LLM
  agentic-compaction.ts       compactación con summarizer
  budget-projection/project.ts proyección determinista y auditable
  summary-generation.ts       prompt/parseo del resumen
core/src/session/models/session-compaction.ts  estado persistido de compactación
```

### 3.1 Presupuesto: request vs mensajes

`compaction-shared.ts:15-37` define los números base:

| Constante | Valor | Rol |
|---|---|---|
| `DEFAULT_MAX_INPUT_TOKENS` | 128 000 | fallback si el modelo no declara límite |
| `CONTEXT_WINDOW_INPUT_RATIO` | 0.9 | input usable si solo hay context window |
| `COMPACTION_TRIGGER_RATIO` | 0.9 | compactar al 90% del input usable |
| `MAX_INPUT_UNDERESTIMATE_FACTOR` | 4 | techo de corrección por subestimación |
| `DEFAULT_TARGET_RATIO` | 0.7 | dejar el transcript al 70% |
| `DEFAULT_PRESERVE_RECENT_TOKENS` | 20 000 | cola viva que no se resume |
| `DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS` | 8 192 | headroom del resumen (modelos que “piensan” se quedaban sin texto) |
| `TOOL_RESULT_CHAR_LIMIT` | 2 000 | chars por resultado de tool antes de resumir |
| `FILE_CONTENT_CHAR_LIMIT` | 2 000 | chars de archivo embebidos en el resumen |
| `MIN_TRUNCATED_MESSAGE_TOKENS` | 8 | mínimo para que truncar valga la pena |

El estimador de Cline asume ~**3 chars/token** (comentario en `:20-25`), más conservador
que el `len/4` de nexus-v3; por eso su trigger del 90% suele disparar antes en texto
denso. La corrección por subestimación existe justamente porque ni 3 chars/token alcanza
para CJK o minificado.

Pero el presupuesto no es uno: hay **request budget** (todo lo que se manda: system
prompt, tools, overhead del provider) y **message budget** (solo el transcript). Cline
traduce entre ambos con `translateRequestBudgetToMessages(requestBudget, overhead)`.
Sin esta traducción, un system prompt grande hace que el “90%” real sea mucho menor.

`resolveAutoRequestTargetTokens` (`compaction.ts:222`) usa 0.7, y para conversaciones
largas baja a `LONG_CONVERSATION_TARGET_RATIO = 0.5` (`:100`): cuanto más largo el
historial, más agresivo el target, porque el costo por request y la probabilidad de
overflow crecen.

### 3.2 Trigger y corrección por subestimación

`compaction.ts:342-362`:

```ts
const actualPreviousInputTokens =
  previousRequestInputTokens > 0 ? previousRequestInputTokens : 0;
const underestimateFactor =
  actualPreviousInputTokens > 0 && requestInputTokens > 0
    ? Math.min(MAX_INPUT_UNDERESTIMATE_FACTOR,
               Math.max(1, actualPreviousInputTokens / requestInputTokens))
    : 1;
const maxInputTokens = rawMaxInputTokens / underestimateFactor;
const requestTriggerTokens = maxInputTokens * COMPACTION_TRIGGER_RATIO;
const shouldCompact = requestInputTokens >= requestTriggerTokens;
```

Propiedades:

- **Solo se activa con evidencia**: el provider reportó para el request anterior más
  tokens que nuestra estimación para el actual.
- **Nunca afloja**: `max(1, ratio)`.
- **Cap 4x**: una estimación diminuta no colapsa el presupuesto.
- **Escala todo el presupuesto**, no solo el trigger: si escalara solo el trigger, la
  compactación arrancaría antes pero retendría demasiado y volvería a overflowear.

Esto es directamente portable. nexus-v3 ya recibe `usage.prompt_tokens` en `LLMResponse`;
falta guardar el último por sesión/modelo y pasarlo a `maybe_compact`. La suite de Cline lo
cubre en `compaction.test.ts` (casos de factor 1, 2, cap 4 y “no aflojar”).

### 3.3 Invariantes de corte (`compaction-shared.ts`)

`isToolResultOnlyUserMessage` (`:289`): un `user` cuyo contenido es **todo**
`tool_result`. No es un turno del usuario; es el resultado de una tool.

`isTurnStartMessage` (`:301`):

```ts
role === "user"
&& metadata.userRunSpan !== 0
&& !isToolResultOnlyUserMessage(message)
&& !isCompactionSummaryMessage(message)
```

`findCutIndex` (`:368`) garantiza:

1. El corte no cae entre un `tool_use` y su `tool_result`.
2. El corte no deja huérfano un `tool_result` cuyo `tool_use` quedó del otro lado.
3. **Nunca se corta antes del último prompt tipado**: si el usuario habló y después hubo
   50 mensajes de tools, el prompt sobrevive. Como mucho, se corta en el prompt.
4. Si no hay ningún turno tipado, el corte puede ser 0 (no compactar) en vez de inventar
   un límite.

`isSafeCutBoundary` acepta un `assistant` o un turno tipado; el resto se ajusta hacia
atrás. `serializeConversation` usa marcadores explícitos para roles y tool blocks, de modo
que el summarizer ve la estructura y no un texto plano.

**Por qué importa:** el bug clásico de “compacté y el agente olvidó la tarea” no es del
modelo; es que el corte dejó el prompt del usuario en la parte resumida. Cline lo prohíbe
por invariant, no por prompt.

### 3.4 `buildBudgetProjection`: el algoritmo exacto

`budget-projection/project.ts:479`. Contrato:

```ts
buildBudgetProjection({
  messages,
  targetTokens,          // > 0 o falla explícito
  policyIntent,          // "agentic_summary" | "basic_compaction_projection" | ...
  estimateMessageTokens,
}) -> {
  status: "ok" | "failed",
  messages,
  actions: BudgetAction[],
  warnings: BudgetWarning[],
  liveTailHandling: "preserved_out_of_band" | ...,
  estimatedTokens,
}
```

Pipeline (en orden):

1. **Clonar** mensajes e índices originales. La proyección nunca muta la entrada.
2. **Resolver política** (`resolveProjectionPolicy`): qué se puede tocar y qué no.
3. **Calcular live tail** (`findProtectedTailStartIndex`, `:145-160`): desde el final,
   busca un `tool_use` sin `tool_result` resuelto. Ese tramo es la “cola viva” de un turno
   en curso y se protege entero.
4. **Podar thinking/binarios fuera del tail** (`_drop_thinking_blocks`,
   `_drop_unsafe_blocks`): `thinking`, `redacted_thinking`, `image`, `media` no se
   reescriben ni se truncan; se eliminan con una acción `dropped_block`. Dentro del live
   tail no se tocan: el modelo los necesita para continuar.
5. **Truncar texto** desde el final (`truncate_message_text`): head + aviso, con un mínimo
   de caracteres. Los tool results tienen un tratamiento especial para no romper JSON.
6. **Descartar desde el inicio** hasta entrar en target:
   - nunca descarta el primer prompt tipado ni el último (`preserved`, `protected_live_tail`),
   - nunca descarta un índice cuya **closure** incluya un pinneado
     (`closureTouchesPinnedMessage`, `:472`),
   - calcula la closure con `collectMessageClosure` (`:162`): unión transitiva de
     `tool_use`/`tool_result` por id. Si sacás un lado, se saca el par completo.
7. **Reintentar truncado** si tras descartar todavía no entra.
8. **Resultado**:
   - `status: "ok"` solo si `estimatedTokens <= targetTokens`,
   - `warnings`: `budget_impossible` (target <= 0) y
     `budget_unachievable_with_protections` (los pines impiden llegar),
   - `actions`: una por cambio, con `path` (índice original), `kind`
     (`truncated_text` | `dropped_block` | `dropped_message` | `preserved`), `reason`,
     `original_size`, `final_size`.
   - `liveTailHandling: "preserved_out_of_band"`: el tail no se contabiliza como recortable.

Detalle de `collectMessageClosure` (`:162-177`):

```
queue = [startIndex]
while queue:
   i = queue.shift()
   if i in removal: continue
   removal.add(i)
   for id in toolIds(messages[i]):
       for linked in pairIndex[id]:
           if linked not in removal: queue.push(linked)
```

`buildToolPairIndex` mapea cada id a todos los índices que lo mencionan (un `tool_use`
puede tener un `tool_result`; un `tool_result` puede aparecer en un mensaje `user` con
varios bloques).

**Garantía formal:** si la salida está en `ok`, entonces (a) todo par está completo,
(b) el primer y el último prompt tipado están, (c) el live tail está, y (d) el tamaño
estimado no supera el target. Si no puede, **no miente**: devuelve `failed` y el caller
decide (fallback agresivo o error).

### 3.5 Compactación básica (sin LLM)

`basic-compaction.ts:452` `runBasicCompaction`. Es la estrategia determinista; se usa
cuando no hay summarizer o como fallback del agentic.

Pasos:

1. Si hay **un solo** mensaje, no compacta (`:461`): truncar el único prompt corrompe el
   request activo.
2. `totalTargetTokens = min(targetTokens, triggerTokens)` (`:464-470`): el target nunca
   puede superar el trigger, o se compactaría para volver a compactar.
3. Recolecta índices de turnos tipados y **sanea** cada uno
   (`sanitizeTypedUserMessage`, `:31`): conserva solo bloques de texto, elimina tool
   results y summaries. Un prompt tipado debe llegar al modelo como texto del usuario.
4. Preserva las **últimas 3 respuestas de assistant con texto**
   (`PRESERVED_ASSISTANT_TEXT_COUNT = 3`, `:61`). Son el “estado del trabajo” que el
   usuario ya vio; resumirlas pierde decisiones recientes.
5. `mergeAdjacentUserTurns` (`:125`): cuando se eliminan mensajes entre dos turnos
   tipados, los fusiona en uno, y agrega al final un bloque de resumen de lo descartado
   (`buildDroppedWorkSummaryBlock`, `:81`) con:
   - resumen de actividad de tools (`summarizeToolActivity`),
   - respuestas preservadas que cayeron en el span.
   Esto evita que queden dos `user` consecutivos (algunos providers lo rechazan) y evita
   perder el hilo de lo que se hizo.
6. Congela salidas de compactaciones previas: un summary anterior no se re-resume.
7. Construye el resultado y lo pasa por **proyección** con:
   - piso `max(target, totalTokens)` (no compactar de más),
   - techo `triggerTokens` (no dejar el transcript al borde),
   - `policyIntent: "basic_compaction_projection"`.

La salida es determinista: mismos mensajes y mismo estimador ⇒ mismo resultado. Eso la
hace testeable y apta para replay.

### 3.6 Compactación agentic (con LLM)

`agentic-compaction.ts:52` `runAgenticCompaction`. Además de resumir, **presupuesta el
request del summarizer**:

- `MIN_AGENTIC_SUMMARY_INPUT_TOKENS = 1_024` (`:29`): si el material a resumir es menor,
  no vale la pena una llamada; se usa básico.
- `buildAgenticSummaryInputBudget` (`:31`) proyecta los mensajes a `targetTokens` con
  `policyIntent: "agentic_summary"` antes de armar el prompt. El resumen no puede
  overflowear al modelo que lo genera.
- `preserveRecentTokens` (20k por defecto) define la cola que no se manda al summarizer.
- Reutiliza el resumen previo si existe y lo pasa como contexto; solo resume lo nuevo.
- Distingue `messagesToSummarize` (histórico) de `newMessagesToFold` (lo que se agregó
  desde el último resumen). Eso permite resúmenes incrementales sin re-resumir todo.
- Marca el mensaje de resumen con `metadata.userRunSpan` coherente y
  `kind: compaction_summary`, para que `isTurnStartMessage` no lo confunda con un pedido
  del usuario.
- Telemetría: `task.compaction_executed` / `task.compaction_skipped` (ver comentario en
  `compaction.ts:253-266`). Nota honesta del propio Cline: las compactaciones hechas por
  plugins o por el hook `beforeModel` **no** emiten esta telemetría.

### 3.7 Estado de compactación de sesión

`session/models/session-compaction.ts`:

- `SessionCompactionStateSchema` (`:25`) persiste el estado (resumen vigente, límites,
  contadores) para que un resume/reload no vuelva a compactar desde cero.
- `projectSessionCompactionState` (`:168`) proyecta el estado al reabrir la sesión,
  reconciliando mensajes nuevos con el resumen persistido.
- `parseSessionCompactionState` (`:200`) tolera estados viejos/parciales.

**Por qué importa:** sin esto, cada reapertura de sesión recompacta y el historial se
degrada. nexus-v3 guarda sesiones en SQLite con FTS, pero no persiste un estado de
compactación; cada proceso re-estima.

### 3.8 Overflow recovery del lado de compactación

`compaction.ts:480-543` implementa el fallback: si la estrategia configurada devuelve
`undefined` o falla, prueba la siguiente (agentic → basic → proyección). El wrapper
`createContextCompactionPrepareTurn` (`:267`) es el que recibe
`previousRequestInputTokens` y `overflowRecovery`, y emite las noticias de estado. La
regla es: **una compactación de recovery por run**, no un loop de “compactar hasta que
entre” (que puede no terminar y quema tokens).

---

## 4. Tools: edición, shell y disciplina de salida

### 4.1 Límites de salida

`output-limits.ts`:

| Constante | Valor | Qué limita |
|---|---|---|
| `MAX_COMMAND_OUTPUT_CHARS` | 48 000 | salida de comandos |
| `MAX_READ_LINES` | 2 000 | líneas por lectura de archivo |
| `MAX_LINE_CHARS` | 2 000 | caracteres por línea (archivos minificados) |
| `MAX_READ_OUTPUT_CHARS` | 48 000 | ventana de lectura |
| `MAX_SEARCH_OUTPUT_CHARS` | 48 000 | resultados de búsqueda |

`truncateCommandOutput` (`:20`) conserva **head + tail** y pone el aviso en el medio
elidido, no en los bordes:

```
<head>\n[... output truncated: N chars total. Refine ...]\n<tail>
```

La razón está documentada en `:10-14`: el builder de requests puede re-truncar con su
propio corte (más agresivo); si el aviso está en el borde, se pierde. Poniéndolo en el
medio elidido, el modelo siempre ve “truncado, refiná el comando”.

Esto es costo, no cosmética: cada carácter vuelve a viajar en **cada** request posterior;
un output de 1 MB al inicio de un run de 50 turnos cuesta ~50 MB de tokens.

### 4.2 `apply_patch`: parser y aplicador

Archivos:

- `core/src/extensions/tools/executors/apply-patch-parser.ts`
- `core/src/extensions/tools/executors/apply-patch.ts`

Formato:

```
*** Begin Patch
*** Update File: src/foo.py
@@ def foo():
-    return 1
+    return 2
*** Add File: src/bar.py
+print("hola")
*** Delete File: src/old.py
*** Update File: src/a.py
*** Move to: src/b.py
@@
 ...
*** End Patch
```

#### Parser (`PatchParser`, `:85`)

- `PATCH_MARKERS` (`:7`): `*** Begin Patch`, `*** End Patch`, `*** Update File:`,
  `*** Add File:`, `*** Delete File:`, `*** Move to:`.
- `BASH_WRAPPERS` (`:18`): `%%bash`, `apply_patch`, `EOF`, ``` ``` ```. El modelo a veces
  envuelve el patch en un bloque de shell o markdown; el parser lo desenvuelve
  (`normalizePatchInput`).
- **Duplicados**: registrar dos veces el mismo archivo (o Update + Delete) es `DiffError`.
  Falla antes de tocar disco.
- **Add sobre archivo existente** es error; **Update/Delete sobre inexistente** es error.
  El parser recibe `currentFiles`, así que valida contra el estado real.
- `*** End of File` es un marcador válido al final de un hunk; se usa para fijar el
  contexto al final del archivo y evitar matches ambiguos.
- Los hunks sin `@@` se aceptan como “buscar este bloque”; con `@@` el texto es una pista
  de contexto, no un número de línea (los números mienten después de la primera edición).

#### Matching difuso

`findContext` (`:388`) y `_find_context` en el port:

1. **Canonicalización Unicode** (`canonicalize`, `:102`): normaliza comillas tipográficas,
   guiones, espacios no rompibles, etc. Un patch escrito por un modelo con “smart quotes”
   aplica igual.
2. **Igualdad exacta** de líneas primero (rápido y sin fuzz).
3. Si no, **similitud** por línea con distancia de Levenshtein
   (`_calculate_similarity`, `:358`), aceptando desde **0.66**.
4. Si el bloque matchea en múltiples lugares, elige el de mayor score; si hay empate,
   avisa (`PatchWarning`) y aplica el primero, o falla según política.
5. `applyChunks` (`:522`) verifica que el contexto **no se solape** con un chunk anterior;
   si se solapa, warning y se salta el hunk (no se corrompe el archivo).
6. Si un hunk no matchea, se agrega un warning con las líneas esperadas vs encontradas;
   el resultado global puede ser “parcial” y se reporta, en vez de escribir a ciegas.

#### EOL y contenido

- `detectLineEnding` (`:108`): CRLF si el archivo lo usa, LF si no.
- `normalizeLineEndings` / `with_eol` (`:591`): el contenido nuevo se ajusta al EOL del
  archivo. Un patch LF sobre un archivo CRLF no genera un diff de todo el archivo.
- Archivo nuevo: usa `os.linesep` (Windows CRLF) salvo que el patch declare otra cosa.
- `computePatchChanges` (`:560`) devuelve `new_content` ya con el EOL correcto y no toca
  disco. La escritura es responsabilidad del caller.

#### Atomicidad

El executor:

1. resuelve **todos** los paths contra el scope,
2. lee el estado actual de todos,
3. parsea y aplica en memoria,
4. solo si todo parseó, escribe,
5. si una escritura falla, restaura los checkpoints de los archivos ya escritos y borra
   los creados.

En Cline el orden es similar; la diferencia es que el executor además emite un diff
numerado y limpia archivos vacíos. En nexus-v3 el `apply_patch_tool` ahora hace
snapshot/restore atómico por archivo (ver §7).

### 4.3 `editor`: diff compacto tras editar

`editor.ts` devuelve, tras una edición, un diff **numerado y acotado** en vez del archivo
completo. El modelo necesita saber qué cambió y en qué línea, no releer 2 000 líneas. Es
la misma filosofía que `output-limits`: el contexto es un recurso.

### 4.4 `bash`: timeouts, árbol de procesos y background

`executors/bash.ts`:

- `timeoutMs` por comando (default 30 000 en la función de conveniencia, `:1138`;
  configurable por tool).
- `killProcessTree` (`:773-817`):
  - Windows: `taskkill.exe /T /F /PID` (mata el árbol); si falla, `child.kill()`,
  - POSIX: `process.kill(-childPid, "SIGKILL")` (grupo de procesos); fallback
    `child.kill("SIGKILL")`.
  Esto evita el clásico “maté el `cmd` pero el `node` hijo sigue corriendo”.
- `EXIT_STREAM_GRACE_MS = 1_000` (`:52`): si el shell salió pero un hijo en background
  mantiene los pipes abiertos, espera 1 s y completa igual, en vez de colgarse hasta el
  timeout. El mensaje avisa que hay procesos en background y que su output ya no se
  captura (`:1093`).
- **Comandos detached** (`detached: !isWindows`, `:737`): si el comando sigue corriendo
  al vencer el timeout de “espera”, se “detacha” y se escribe a un log temporal
  (`cline-command-*/output.log`), con un marcador `active-command.json`
  (pid + process start token + timestamp). El modelo recibe la ruta y puede seguir
  leyendo. Limpieza: retención 24 h, 10 MB por log, reaping con verificación de que el
  pid sigue siendo el mismo proceso (evita matar un pid reciclado).
- `MAX_COMMAND_OUTPUT_CHARS` se aplica en streaming; el buffer no crece sin límite.
- `TimeoutError` incluye `timeoutMs` para que el mensaje sea accionable.

### 4.5 `command-guard`: plan mode real

`extensions/tools/command-guard.ts`.

Problema: en plan mode (read-only) hay que permitir `git status`, `grep`, `ls`, `npm test`,
pero bloquear `rm`, `git commit`, `npm install`, redirecciones a archivo, `sed -i`, etc.
Bloquear `exec` entero es inútil; permitirlo entero es peligroso.

Algoritmo (`findFileEditingCommand`, `:479`):

1. **Enmascarar** texto que no es comando (`maskNonCommandText`): contenido de quotes,
   heredocs, comentarios. Sin esto, `echo "rm -rf /"` se bloquea y `echo x; rm -rf /` no.
2. **Split** por operadores de shell (`;`, `&&`, `||`, `|`, saltos de línea) respetando
   el masking.
3. **Tokenizar** cada segmento.
4. **Saltar prefijos** (`_skip_prefix_tokens`): asignaciones de entorno (`FOO=1`),
   `sudo`, `env`, `timeout 30`, `xargs`, `command`, `nohup`, etc. Un wrapper no debe
   esconder el comando real.
5. **Chequear**:
   - `BLOCKED_COMMANDS` (`:23`): `rm`, `mv`, `cp`, `mkdir`, `touch`, `chmod`, `chown`,
     `ln`, `truncate`, `dd`, `tee`, `sed` (según flags), `perl -i`, `python -c` con
     escritura, etc.
   - `BLOCKED_SUBCOMMANDS` (`:152`): `git add/commit/push/...`,
     `npm install/uninstall/...`, `pip install/...`, `cargo add/...`, `go get/...`,
     `dotnet add/...`, `winget install/...`, etc.
   - flags in-place (`sed -i`, `perl -pi`),
   - **redirecciones** (`_REDIRECT`): `>`/`>>`/`&>` a un archivo. Se permiten solo
     destinos seguros (`/dev/null`, `NUL`, `$TEMP`, `%TEMP%`,
     `is_allowed_redirect_target`).
   - **shells anidados**: `cmd /c`, `bash -c`, `powershell -Command`, `sh -c` se
     re-parsean recursivamente (si no, `cmd /c "del x"` pasaría).
6. Devuelve el motivo (nombre del comando) o `None`.
7. `formatPlanModeBlockedCommandError` (`:514`) explica el bloqueo y el escape hatch
   (escribir en `%TEMP%` / `/tmp`) para que el modelo no reintente en loop.

**nexus-v3** ahora usa esto en `Scope.check_tool` para `read_only`: los shell tools
pasan solo si `find_file_editing_command` devuelve `None`. El `_exec_jail` sigue
bloqueando meta-caracteres peligrosos (`[;&|`<>]`, `$(`, `${`), así que hay **dos**
capas: el guard semántico y el jail sintáctico.

---

## 5. Hooks: extensibilidad por archivo con control acotado

Archivos:

- `hooks/hook-file-config.ts`: descubre y valida archivos de hook.
- `hooks/hook-file-hooks.ts`: los ejecuta y mapea a eventos.
- `hooks/subprocess.ts`: protocolo, timeout, parseo de control.
- `hooks/checkpoint-hooks.ts`: hooks de checkpoint (captura/restore).

### 5.1 Eventos

Los eventos cubren el ciclo completo: `agent_start`, `agent_resume`, `agent_end`,
`user_prompt_submit`, `tool_call`, `tool_result`, `task_start`, `task_complete`,
`task_cancel`, `session_shutdown`, etc. Cada evento tiene un payload tipado
(`HookEventPayloadSchema`) con `agentId`, `conversationId`, `iteration`, `workspaceInfo`
(branch/commit/remote), tool name/args/result, etc.

### 5.2 Descubrimiento

`hook-file-config.ts:17` define la convención: archivos por evento (p. ej.
`PreToolUse.py`) en un directorio de hooks del workspace/usuario. Se validan nombres y se
resuelve el intérprete por extensión. No se ejecuta cualquier cosa: solo archivos
reconocidos y dentro de las raíces permitidas.

### 5.3 Protocolo

`subprocess.ts`:

- El hook recibe el payload por **stdin** como JSON y variables de entorno
  (`CLINE_USER_ID`, `CLINE_VERSION`, `CLINE_HOOK_AGENT_RESUME`, etc.).
- Su **stdout** puede ser:
  - vacío o texto: se ignora (o se usa como contexto, según evento),
  - **JSON de control** (`HookOutputSchema`, `:202`): `cancel`, `contextModification`,
    `stop`, etc.
- stdout inválido:
  - en `tool_call`/`tool_result`: es **error** (`:535-540`), porque un control malformado
    no debe interpretarse como “permitir”,
  - en `agent_start`: se tolera (`:454-460`), porque históricamente era fire-and-forget.
- **Timeout duro**: `DEFAULT_TOOL_HOOK_TIMEOUT_MS = 120_000` (`:131`). Sin timeout, un
  hook colgado bloquea el agente para siempre.
- **Contexto acotado**: `MAX_HOOK_CONTEXT_SIZE = 50_000` (`:56`); el excedente se corta
  con aviso (`:61-64`). Un hook no puede inundar el contexto.
- **Run-start hooks** son fire-and-forget por defecto; `blockingRunStartHooks` (`:169`)
  los vuelve bloqueantes, con la advertencia de que los hooks largos deben backgroundearse.
- Errores de dispatch se reportan por `onDispatchError`; un hook roto no debe tumbar el run.

### 5.4 Seguridad

- El hook corre como subproceso del usuario: **no hay sandbox de SO**. La defensa es
  descubrimiento acotado + timeout + payload estructurado + no pasar secretos.
- El control JSON se parsea con schema; campos desconocidos se ignoran.
- `abort` se mapea a `agent_abort` (`:625`), no a error genérico.

**nexus-v3 (implementado):** `src/core/file_hooks.py` descubre scripts en
`.nexus/hooks/<fase>/` (o `.nexus/hooks/<fase>.<ext>`), los ejecuta con
`run_process_capture` (stdin JSON, stdout JSON, timeout y kill de árbol) y los registra en
`HooksEngine`. Convenciones: exit 2 = STOP; exit 0 sin JSON = CONTINUE; `fail_closed`
configurable. El directorio es opcional y debe resolver dentro del workspace. Config:
`hooks.files.{enabled,dir,timeout_s,fail_closed}`. Tests: `tests/test_file_hooks.py`.
La diferencia con Cline es el vocabulario de fases (las 6 del engine, no los 18 eventos) y
que el payload no incluye secretos ni credenciales.

---

## 6. Checkpoints: git como motor de snapshots

Archivos: `session/checkpoint-restore.ts`, `session/checkpoint-diff.ts`,
`hooks/checkpoint-hooks.ts`.

### 6.1 Captura

Cline no copia archivos: crea un **stash commit** en un ref privado. La clave está en
`createWorktreeStashCommit`: el commit tiene un **tercer padre** con el árbol de
untracked (`refs/cline/...`). Un stash normal de git (`stash create`) **omite untracked**,
y un restore con `git clean -fd` los borraría. El tercer padre permite restaurarlos.

- `beginWorktreeRestoreTransaction` (`:50`): antes de un restore destructivo, captura el
  worktree actual con `stash push --include-untracked`, lo mueve a un ref privado
  (`refs/cline/restore-transactions/<id>`) y recién entonces toca el worktree.
- Si el restore falla, rollback: `reset --hard <originalHead>`, `clean -fd`,
  `stash apply --index <privateRef>`, borrar el ref (`:90-150`).
- El ref privado no aparece en `git stash list` del usuario.

### 6.2 Restore

`applyCheckpointToWorktree` (`:376`):

1. verifica que `cwd` sea un repo git,
2. resuelve el `restoreBase` (commit o stash),
3. `git reset --hard <base>` **solo si** el HEAD actual es el esperado; si el usuario
   cambió de rama, no mueve la rama por sorpresa (`:403-408`),
4. `clean -fd` para eliminar archivos creados después,
5. si el snapshot tiene tercer padre, restaura untracked,
6. si es un snapshot viejo de 2 padres, deja los untracked intactos (no puede
   restaurarlos) y lo informa (`:338-341`).

`trimMessagesToCheckpoint` (`:245`) y `trimMessagesBeforeUserRun` (`:253`) recortan el
transcript al punto del checkpoint, para que “restore” sea coherente: no solo vuelven los
archivos, también la conversación.

### 6.3 Diff

`checkpoint-diff.ts`:

- `runGit diff --name-only -z <ref> --` para listar archivos cambiados (NUL-separated,
  a prueba de espacios).
- `buildCheckpointWorkspaceDiff` (`:150`): por archivo, contenido del checkpoint vs
  workspace, con `git show`/lectura.
- **Path escape check** (`:48`): si un path relativo se sale del workspace, error. Un
  checkpoint no puede leer `/etc/passwd`.
- `compareCheckpointToWorkspace` (`:171`) devuelve solo los diffs con contenido distinto.

### 6.4 Lo implementado en nexus-v3

`src/core/git_checkpoints.py` replica el concepto con un **repo sombra** en
`data/git_checkpoints/<sesión>` (`NEXUS_GIT_CHECKPOINTS_DIR` para cambiarlo), sin tocar el
repo del usuario:

- `git add -A` sobre un work-tree que apunta a la raíz del proyecto captura tracked +
  untracked, respetando `.gitignore` y una lista de exclusiones (`data/`, `brain/`,
  `node_modules/`, venvs, builds, DBs, binarios y medios);
- el commit se crea con `commit-tree` + `update-ref HEAD`, así que no hay commits en la
  rama del usuario ni se mueve su índice;
- `checkpoint_list/diff/restore` (tools y `/api/checkpoints/git`); `restore` exige
  `confirm=true` y `clean=true` borra los archivos posteriores (las exclusiones se
  respetan);
- `apply_patch` crea un checkpoint best-effort antes de escribir varios archivos
  (`checkpoints.git.auto_before_apply_patch`).

No se portó el tercer padre del stash ni la transacción de restore con rollback: el repo
sombra hace innecesario el stash y el checkpoint por archivo sigue cubriendo el undo fino.
Tests: `tests/test_git_checkpoints.py`.

---

## 7. Tests de Cline que fijan estos contratos

No son tests de UI: son tests de invariantes. Los más relevantes:

| Test | Qué fija |
|---|---|
| `context/budget-projection/project.test.ts` | target <= 0 falla; pines; closure; truncado; warnings; live tail |
| `context/compaction.test.ts` | trigger 90%, factor de subestimación (1/2/cap 4), no aflojar, overflow recovery |
| `context/compaction.live.test.ts` | compactación contra provider real (skip si no hay credenciales) |
| `context/summarize-conversation.test.ts` | formato del resumen, merge de turnos, prompts tipados |
| `tools/command-guard.test.ts` | blacklist, subcomandos, wrappers, quotes/heredocs, redirecciones, shells anidados |
| `tools/command-guard-extension.test.ts` | integración con plan mode |
| `tools/executors/apply-patch.test.ts` | Add/Delete/Move, fuzzy, EOL, EOF, duplicados, atomicidad |
| `tools/executors/bash.test.ts` | timeout, kill de árbol, output limits, background |
| `tools/executors/bash.powershell.test.ts` | invocación de PowerShell en Windows |
| `tools/executors/output-limits` (vía bash/file-read/search) | head+tail, aviso, caps |
| `hooks/subprocess-runner.test.ts` | timeout, control JSON, stdout inválido, abort |
| `hooks/hook-file-config.test.ts` | descubrimiento y validación de archivos |
| `hooks/hook-file-hooks.test.ts` | mapeo evento → archivo → resultado |
| `hooks/checkpoint-hooks.test.ts` | captura/restore de checkpoint |
| `session/checkpoint-restore.test.ts` | transacción, untracked, rollback, trim de mensajes |
| `session/checkpoint-diff.test.ts` | names, contenido, path escape |
| `session/models/session-compaction.test.ts` | persistencia y proyección del estado |
| `session/services/message-builder.test.ts` | truncado de backstop al construir el request |
| `agents/agent-runtime.test.ts` | loop, retries, max-tokens, overflow, tool groups, hooks |

La lección de método: Cline testea **propiedades** (“nunca queda un par huérfano”, “el
request de recovery es estrictamente menor”), no solo casos felices. Los ports de
nexus-v3 siguen esa línea.

---

## 8. Comparación con nexus-v3 (antes / después de este lote)

### 8.1 Compactación

| Aspecto | nexus-v3 antes | nexus-v3 ahora | Cline |
|---|---|---|---|
| Corte | últimos N mensajes (`keep_recent`) | `keep_recent` + **nunca antes del último prompt tipado** | `findCutIndex` con boundary seguro |
| Pares tool | podía orphanar | proyección con closure | closure transitiva |
| Garantía de tamaño | `> 2*limit` recién rechaza | proyección a `limit`, `failed` explícito | `status ok/failed` |
| Auditoría | nombres de capa | `actions` con motivo y tamaños | `actions` |
| System prompt | se preservaba por prefijo | **pinneado por rol** en la proyección | fuera del transcript / protegido |
| Último prompt | se perdía si el loop tenía muchas tools | se reinyecta y, si no entra, se recorta con aviso | nunca se recorta |
| Salida del resumen | podía superar el límite | se proyecta al límite | presupuesto propio del summarizer |

Implementación:

- `src/core/compaction_budget.py` (801 líneas): proyección, políticas, pines, closure,
  truncado, acciones, warnings. Equivalente a `project.ts` con adaptaciones al modelo de
  mensajes de nexus.
- `src/core/compaction.py`: `_estimate_one` (suma exacta), `_snip` con pin del prompt,
  L4 reemplazado por proyección + fallback, `_build_summary_output` reinyecta el prompt y
  proyecta.

**Desviación deliberada:** Cline nunca trunca el último prompt; nexus lo trunca **solo**
si el prompt solo no entra en el límite del resumen, con el marcador
`[...prompt recortado para entrar en el resumen]`. La alternativa era perderlo o fallar
la compactación entera. Está aislado en `_build_summary_output` y no afecta a la
proyección genérica.

### 8.2 `apply_patch`

| Aspecto | antes | ahora |
|---|---|---|
| Operaciones | solo `Update File` | `Add` / `Delete` / `Update` / `Move to` |
| Matching | exacto, 1 ocurrencia | exacto → fuzzy 0.66 con canonicalización Unicode |
| EOL | lectura con newline universal (rompía CRLF) | `newline=""` en lectura y escritura, EOL detectado |
| `*** End of File` | no soportado | soportado |
| Multi-archivo | atómico por validación | atómico con snapshots + rollback + borrado de creados |
| Diagnóstico | “coincide N veces” | warnings por hunk, fuzz, archivo/línea esperada |

Implementación: `src/core/apply_patch.py` (632 líneas), integrado en
`src/api/server.py::apply_patch_tool`. `dry_run` devuelve archivos, operaciones, moves y
fuzz sin escribir.

### 8.3 Plan mode / shell

| Aspecto | antes | ahora |
|---|---|---|
| `exec` en `read_only` | bloqueado entero | permitido si el comando no edita |
| Detección | ninguna | blacklist + subcomandos + wrappers + redirecciones + shells anidados |
| Defensa en profundidad | `_exec_jail` | `_exec_jail` + `command_guard` |
| Mensaje al modelo | “denegado” | explica plan mode y escape hatch |

Implementación: `src/core/command_guard.py` (~330 líneas) + `Scope.check_tool`.

### 8.4 Lo que Cline tiene y nexus-v3 todavía no

| Mecanismo | Impacto | Esfuerzo | Estado / nota |
|---|---|---|---|
| Underestimate factor con `usage` real | Alto | S | **Portado**: `agent_loop.record_usage_calibration` + providers reportan `prompt_tokens` |
| Overflow recovery con “estrictamente menor” | Alto | S | **Portado**: `reactive_compact` compara longitud y no reintenta si no encogió |
| Compactación agentic con presupuesto del summarizer | Medio | M | **Portado**: `summary.input_max_tokens` + proyección del `middle` |
| Kill de árbol de procesos en `exec` | Medio | S | **Portado**: `src/core/process_tree.py` (`taskkill /T` / `killpg`) |
| Hooks por archivo | Medio | M | requiere subprocess + schema |
| Checkpoints git con untracked | Medio | L | el modelo actual por archivo es suficiente para la mayoría |
| Estado de compactación persistido | Medio | M | evitaría recompactar al reabrir |
| Team tools (spawn/await/mailbox) | Bajo/Medio | L | nexus ya tiene board + subagentes |

---

## 9. Riesgos, límites y decisiones de diseño

1. **No copiar el modelo de bloques.** Cline usa content blocks; nexus usa strings +
   `tool_calls`. Migrar todo habría tocado providers, sesiones y UI. El port adapta en los
   bordes (`_content_blocks`, `collect_tool_ids`) y mantiene el core en el formato actual.
2. **`preserve_roles=("system",)` es una desviación.** En Cline el system prompt suele ir
   fuera de `messages`; en nexus vive adentro. Sin el pin, la proyección podía borrar las
   instrucciones del sistema. Documentado y testeado.
3. **El estimador sigue siendo `len/4`.** La proyección es correcta **relativa** a ese
   estimador. El factor de subestimación (ya portado) corrige el presupuesto con el
   `prompt_tokens` real del provider; sin ese dato (Ollama viejo, proveedor sin usage) el
   sistema vuelve al comportamiento conservador anterior.
4. **La compactación no es gratis.** Resumir cuesta una llamada y puede perder matices.
   Por eso Cline preserva prompts, respuestas finales y salidas previas, y por eso nexus
   solo compacta cuando se supera el umbral.
5. **`_summary_fallback` sigue existiendo.** Si la proyección falla (p. ej. el system
   prompt + prompt actual solos superan el límite), se usa el fallback agresivo. Es el
   último recurso, no el camino feliz.
6. **El guard de comandos es heurístico.** No es un sandbox: un binario arbitrario puede
   editar archivos sin que el nombre lo delate (`python script.py`). Para plan mode real
   hace falta sandbox de SO o allowlist de comandos; el guard reduce el 95% accidental,
   no el 100% adversarial. `_exec_jail` y el scope siguen siendo la barrera de paths.
7. **Los hooks por archivo son código del usuario.** Sin sandbox, un hook es un
   subproceso con los permisos del usuario. Cline lo mitiga con timeout y schema, no con
   aislamiento. Si se porta, hay que decidir si se aceptan hooks de workspace por defecto
   (riesgo de repo malicioso) o solo de usuario.
8. **Los checkpoints git mueven el worktree.** `reset --hard` + `clean -fd` pueden borrar
   trabajo no commiteado si la transacción falla. Cline lo mitiga capturando antes en un
   ref privado; aun así es la operación más riesgosa del sistema. No se portó.

---

## 10. Plan de port restante (priorizado)

### P0 — cerrar el ciclo de contexto (S/M) — **hecho**

1. **Underestimate factor** ✅
   - `agent_loop.record_usage_calibration` guarda `factor = clamp(prompt_tokens/estimado, 1, 4)`
     por sesión y `_effective_context_max_tokens` divide el presupuesto completo.
   - `providers/ollama_provider.py` ahora reporta `prompt_tokens`/`completion_tokens`.
   - Test: `tests/test_context_recovery.py` (2x, cap 4x, nunca afloja).
2. **Overflow recovery con verificación de tamaño** ✅
   - `reactive_compact` compara longitud serializada antes/después y lanza si no encogió;
     `agent_loop` no reintenta en ese caso.
   - Test: request ya mínimo ⇒ `RuntimeError`, no segundo request.
3. **Presupuesto del resumen** ✅
   - `summary.input_max_tokens` (default 24000) y proyección determinista del `middle`
     antes de serializarlo; el `input_target` tiene piso 1024.
   - Test: historial enorme ⇒ el summarizer recibe ≤ 6k chars con target 1024.

### P1 — robustez de tools (S/M) — **hecho**

4. **`apply_patch` en la UI** ✅ — `ui/src/components/chat/ApplyPatchResult.tsx` parsea el
   JSON y muestra dry-run/aplicado, archivos por operación, fuzz y checkpoint git.
5. **`output-limits` unificado** ✅ — `list_dir`, `glob` y `code_search` usan
   `cap_json_list` (recorta la lista manteniendo JSON válido) con aviso accionable.
6. **Kill de árbol en `exec`** ✅ — `src/core/process_tree.py` (`taskkill /T /F` en Windows,
   `killpg` en POSIX) con salida parcial en el error. Test de timeout incluido.

### P2 — extensibilidad (M/L) — **hecho**

7. **Hooks por archivo** ✅ — `src/core/file_hooks.py`: descubrimiento, subprocess con
   timeout, control JSON, exit 2 = STOP y `fail_closed` configurable. Test:
   `tests/test_file_hooks.py`.
8. **Estado de compactación persistido** ✅ — tabla `compaction_state` en `session.db`
   (`tokens_before/after`, `saved`, capas, resumen y contador); `AgentLoop` la escribe en
   cada compactación y `_summary_state` la usa como fallback. Endpoint:
   `GET /api/compaction/state?session_id=`. Test: `tests/test_compaction_state.py`.
9. **Checkpoints git** ✅ — conviven: snapshot por archivo para undo fino y repo sombra
   para restore del workspace completo (untracked incluidos). Ver §6.4.

### P3 — no urgente

10. **Team tools** ✅ — `src/core/team.py` + tools `team_spawn` (background), `team_await`,
    `team_mailbox` y `team_list` sobre `SubAgentSpawner`. Test: `tests/test_team.py`.
11. Compactación con `displayRole`/`kind` para que la UI no muestre resúmenes como
    mensajes del usuario. **Sigue pendiente** (requiere cambiar el schema de mensajes y
    el render de la UI).

---

## 11. Apéndice: invariantes que conviene testear siempre

- [ ] La proyección nunca deja un `tool_use` sin `tool_result` ni viceversa.
- [ ] La proyección nunca elimina el primer ni el último prompt tipado.
- [ ] La proyección nunca elimina mensajes `system` (en nexus).
- [ ] Si `status == "ok"`, `estimated_tokens <= target`.
- [ ] Si no puede, `status == "failed"` y hay warning; no hay éxito silencioso.
- [ ] `compact()` no corta antes del último prompt tipado.
- [ ] `_build_summary_output` no supera `limit` (o devuelve `None`).
- [ ] `apply_patch` no escribe nada si un archivo falla.
- [ ] `apply_patch` preserva el EOL del archivo.
- [ ] `dry_run` no modifica el filesystem.
- [ ] `read_only` + comando que edita ⇒ `ScopeDenied` con explicación.
- [ ] `read_only` + comando de solo lectura ⇒ permitido.
- [ ] Overflow recovery no reintenta con un request igual o mayor.
- [ ] Max-tokens recovery no replaya un turno con tool calls o side effects.

---

## 12. Glosario mínimo

- **Turno tipado**: mensaje `user` escrito por el humano (no tool result, no summary, no
  inyección). En Cline: `userRunSpan !== 0`.
- **Live tail**: cola de mensajes desde el último `tool_use` sin resolver; es un turno en
  curso y no se puede resumir sin romperlo.
- **Closure**: conjunto transitivo de mensajes conectados por ids de tool; se elimina
  entero o no se elimina.
- **Proyección**: transformación determinista del transcript para entrar en un
  presupuesto, con acciones auditables.
- **Compactación básica**: sin LLM; preserva prompts/respuestas y resume actividad de
  tools.
- **Compactación agentic**: con LLM; presupuesta su propio request y reutiliza el resumen
  previo.
- **Underestimate factor**: corrección del presupuesto cuando el provider reporta más
  tokens que la estimación.
