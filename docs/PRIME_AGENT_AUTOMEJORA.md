# Cómo se mejora solo prime-agent — lectura del código, no del README

> Alcance: `prime-agent` clonado en `%TEMP%\opencode\prime-agent` (Rust + runtime Python).
> Todo lo que sigue sale de leer `crates/pa-core/src/refinement/*`,
> `crates/pa-core/src/session_engine/{refine,auto_refine_trigger,refinement_gate,harness_digest}.rs`,
> `prime-agent-runtime/src/rlm/harness.py` y `skills/{refine,goal,compact,factory}`.
> Cada afirmación tiene su archivo en §9.
>
> Complementa `PRIME_AGENT_AUTOPSY.md` (que es el mapa rápido). Esto es el detalle.

---

## 0. La palabra "automejora" tapa tres cosas distintas

El README dice *"A Self-Improving RLM Harness"* y en la misma lista mete el REPL, el
continual harness, las skills ejecutables y los goals. Son mecanismos separados, con
riesgos separados. Conviene no confundirlos:

| Capa | Qué agrega | Dónde vive | ¿Es memoria? |
|---|---|---|---|
| **Runtime** (RLM) | el modelo programa: contexto como variable, subagentes como llamadas | REPL de Python persistente | no: es capacidad de acción |
| **Estado** (continual harness) | lo que el agente sabe y hace sobrevive al historial de tokens | `harness_state.json` | **sí: es el corazón** |
| **Meta** (skills/factory/goal) | procedimientos que se guardan, se ejecutan y se vuelven a usar | paquetes Python + specs | parcial: son procedimientos |

`/refine` es **solo la capa 2**. Es el único mecanismo que cambia el comportamiento del
agente sin que un humano escriba el cambio, y por eso es el único que necesita gates.

La tesis del diseño, en una línea: *la compactación resume el pasado para no perderlo; el
refine extrae del pasado un artefacto reutilizable para no repetirlo.* Misma entrada (la
trayectoria), misma salida (texto), distinto destino: la compactación produce contexto
desechable, el refine produce estado durable.

---

## 1. El artefacto: cinco tipos de entrada, tres acciones

```
REFINEMENT_KINDS = ["prompt", "memory", "skill", "subagent", "factory"]
acciones          = ["create", "update", "delete"]
```

Los cinco tipos no son sinónimos. Cada uno tiene un **contrato de validación distinto** y
un lugar distinto en el prompt:

| Tipo | Qué es | Contrato extra | Cuándo se crea |
|---|---|---|---|
| `prompt` | nota suplementaria de conducta ("si tocás X, primero Y") | `id != base_system_prompt` | una política angosta, no una tarea |
| `memory` | hecho, decisión, preferencia, fracaso, resultado | ninguno | dato que se va a volver a necesitar |
| `skill` | procedimiento invocable desde el kernel | `reference` + `arguments` obligatorios | procedimiento que se repitió |
| `subagent` | rol de delegación reutilizable | ninguno (pero el prompt exige la forma `rlm.spawn`) | rol que se repitió |
| `factory` | máquina de estados de subagentes | `arguments.dag` XOR `arguments.machine` | workflow con forma (fan-out, loops, joins) |

La distinción importa porque el prompt del planificador la repite explícitamente:
*"creá o actualizá el componente más chico que sirva: roles de delegación repetidos →
subagent, procedimientos repetidos → skill, hechos durables → memory, políticas angostas →
prompt"*. Es una regla anti-inflación: sin ella, todo termina siendo un `prompt` gigante.

### Formato en disco (`harness_state.json`, schema 1)

```json
{
  "schema": 1,
  "entries": {
    "memory": {
      "prefer_ruff": {
        "id": "prefer_ruff",
        "kind": "memory",
        "title": "Formatear con el ruff del proyecto",
        "content": "El venv del repo trae ruff; usarlo antes de commitear.",
        "path": "tooling",
        "scope": "local",
        "reference": {},
        "arguments": {},
        "metadata": {},
        "source": "refine",
        "created_at": "2026-10-27T18:02:11Z",
        "updated_at": "2026-10-27T18:02:11Z",
        "version": 1
      }
    },
    "prompt": {}, "skill": {}, "subagent": {}, "factory": {}
  },
  "refinements": [ { "id": "refine_20261027180322123", "trigger": "...", "changes": ["create memory:prefer_ruff"], "evidence": "...", "outcome": "...", "created_at": "..." } ]
}
```

Detalles que parecen cosméticos y no lo son:

- **`version`** se incrementa en cada update (`before.version + 1`). Sirve para ver desde la
  UI cuántas veces se tocó una entrada sin diffear el contenido.
- **`created_at` se preserva** en el update; solo `updated_at` avanza. Sin eso, "antigüedad"
  deja de ser medible.
- **Los campos que el edit no trae se heredan del `before`**. Un update parcial no borra
  `path`, `metadata` ni `reference`: `title.or(before.title)`, `content.or(before.content)`, etc.
- **`scope` también se hereda** del `before` antes de caer al scope de la corrida. Una
  entrada global no se vuelve local por un update.
- **`source: "refine"`** distingue lo que escribió el refinador de lo que escribió el agente
  a mano desde el kernel (`source: "agent"`).
- **`refinements[]` dentro del state** es el resumen que entra al prompt; el detalle con
  `before`/`after` por edit va aparte, en `refinement_history.jsonl` (append-only). Es la
  separación entre *lo que el modelo necesita leer* y *lo que hace falta para deshacer*.

---

## 2. Dos alcances: local (la sesión) y global (el agente)

| | Local | Global |
|---|---|---|
| Directorio | dir de artefactos de la sesión (`RLM_HARNESS_STATE_DIR`, o `$RLM_SESSION_DIR/harness`) | `<agent dir>/harness` |
| Vive | mientras viva la sesión persistida | entre sesiones y proyectos |
| Default | **sí** | hay que pedirlo (`/refine --global`, o `refine.run(global_=True)`) |
| Puede tocar | solo lo local | solo lo global |
| Falla si | la sesión no se persiste | el dir no se puede crear |

La regla que hace que esto funcione es asimétrica y explícita en el prompt: **en un refine
local, las entradas globales son contexto de solo lectura**. El refinador no puede
proponer update/delete sobre ellas; si necesita una excepción, crea una entrada local que
la pise. Al revés no hay restricción, porque el global no depende del local.

El merge es donde aparece el detalle fino: al combinar global + local para el prompt, las
entradas locales que colisionan de id se muestran con prefijo **`local:`** (display-only).
El id real no cambia; el prefijo existe para que el modelo no edite el global creyendo que
edita el local. En el JSON `scope: "local"` y `id: "m1"`; en el prompt, `[local:m1]`. Y al
aplicar, `_strip_scope_prefix` acepta `local:m1` y `global:m1` y rutea al store correcto,
porque los modelos copian el id tal como lo ven.

El criterio de promoción también está escrito: se permite global un aprendizaje de
proyecto **solo si** el título, el path o el contenido nombran explícitamente el proyecto y
la lección se va a reusar en sesiones futuras de ese proyecto. Si no, es local.

---

## 3. Cómo se escribe sin corromperse (la parte que casi nadie hace)

Hay dos escritores reales sobre el mismo archivo: el **kernel** (el `HarnessState` que vive
en memoria dentro del proceso Python del agente) y el **host** (`/refine`, en otro proceso).
Sin disciplina, el próximo `save()` del kernel pisa el refine del host con un snapshot
viejo. Lo que hay es, en orden:

1. **Lock de directorio**, no de archivo: `mkdir` sobre `harness_state.json.lock` (atómico en
   todos los SO). Dentro, un archivo `owner` con `"<pid> <token>.<uuid>"`.
2. **Detección de lock huérfano**: si el lock tiene más de 10 s (`_LOCK_STALE_AFTER`), se lee
   el `owner` y se pregunta si el proceso vive (`os.kill(pid, 0)`). Si está muerto, o el
   `owner` es ilegible, se borra y se reintenta. Un `PermissionError` cuenta como **vivo**:
   ante la duda, no se roba el lock.
3. **Espera acotada**: 50 intentos cada 20 ms (~1 s). Pasado eso, `RuntimeError`. Preferible
   fallar el refine a colgar el turno.
4. **Sync por mtime**: antes de cada operación bajo lock, si `st_mtime_ns` del archivo no es
   el que se registró al cargar/guardar, se recarga. Es el mecanismo que hace que el kernel
   vea los edits del host.
5. **Escritura atómica**: temp en el mismo directorio con `O_CREAT|O_EXCL`, `json.dump`,
   `flush`, `fsync` (en macOS `F_FULLFSYNC`), y `os.replace`. En POSIX además `fsync` del
   directorio. Un lector nunca ve un archivo a medias.
6. **Permisos**: el temp se crea con el modo del destino, o `0600`. Nunca más laxo.
7. **Verificación de propiedad antes del rename**: si el `owner` del lock ya no es el
   nuestro, se aborta con `lock lost` en vez de escribir. Es la defensa contra un lock
   robado por staleness justo mientras se escribía.
8. **Archivo corrupto = archivo vacío**: `json.load` sobre basura devuelve `{}`, y el `save()`
   siguiente lo regenera limpio. Un JSON roto no puede dejar al agente sin arrancar.

El nivel de prueba de esto es la señal de cuánto les importa: un test corre **40 rondas de
dos hilos** haciendo read-modify-write sobre el mismo state y exige que queden las 80
entradas. Otro verifica que el save emite exactamente un `fsync`. Eso no es un detalle de
implementación; es el requisito de que la automejora no se coma a sí misma.

---

## 4. El ciclo completo de `/refine`

### 4.1 Tres disparadores, un solo embudo

| Origen | Cómo entra | Cuándo corre |
|---|---|---|
| usuario | `/refine [instrucciones]` en el cliente | inmediato, en el límite de turno |
| modelo | `await refine.run(instructions, global_=False)` desde el REPL | **al terminar el turno**, nunca a mitad de celda |
| automático | compactación exitosa arma el trigger | en el próximo checkpoint o al disponer la sesión |

El del modelo devuelve `{"scheduled": True}` al instante y sigue: el refinamiento no corre
mid-turno, corre cuando el turno cierra. Una segunda llamada en el mismo turno no encola
otra corrida, **solo actualiza las instrucciones**. Es la diferencia entre "pedí dos veces"
y "pedí dos refinamientos".

Los tres terminan en `execute_refinement()`, que es el único embudo: transcript, dirs,
modelo, opciones, source y gates viajan juntos. No hay una segunda ruta de escritura.

### 4.2 Los gates, en orden (y en qué orden importa)

```
enabled  →  turn_interval  →  compact  →  cooldown  →  surface  →  in_flight  →  branch_version
```

| Gate | Default | Qué evita |
|---|---|---|
| `enabled` (master switch) | on | apagar todo sin borrar historial; se lee **sin caché** en cada operación, desde `settings.json` |
| `turn_interval` | 25 (mínimo 1) | refinar cada turno, que es ruido |
| `compact` | true | correr el auto-refine cuando el usuario desactivó el trigger por compactación |
| `cooldown_ms` | 20 min | dos reviews seguidos sobre la misma evidencia |
| `surface` | — | ver abajo |
| `in_flight` | — | dos corridas pisándose: la segunda re-arma el trigger |
| `branch_version` | — | aplicar un review aprobado sobre una rama que el usuario ya abandonó |

El orden es significativo: los gates baratos van antes de la llamada al LLM. Un trigger que
muere en `enabled` o `compact` se descarta en silencio y **no** gasta tokens.

La distinción de superficie es sutil y útil:

- **Checkpoint** (entre turnos): un trigger bajo cooldown **queda pendiente** para un
  boundary posterior. Nada se pierde, solo se posterga.
- **Dispose** (la sesión se cierra): un trigger bajo cooldown **se descarta sin review**; uno
  fresco corre su review. No tiene sentido arrancar un refine de 30 s en un cierre.

Y hay un caso intermedio: si el review aprobó pero hay un turno activo, el review se
**retiene** (`pending_review`) y el próximo boundary corre el refinement sin volver a
llamar al LLM. Se paga un review, no dos.

El cooldown y los contadores viven en memoria (`CompactAutoRefineState`), no en disco. Al
reiniciar el proceso, el contador de turnos vuelve a cero y el cooldown se olvida. Es una
decisión razonable (evita escribir un archivo por turno) con una consecuencia concreta:
reiniciar el agente en bucle puede disparar más reviews de los que el `cooldown_ms` sugiere.

### 4.3 El review gate: un segundo LLM decide si vale la pena

Antes de planificar, otra llamada al modelo responde una pregunta binaria. El prompt
completo es corto y vale leerlo entero:

> *"Sos el gate de review automático de /refine. Decidí si este checkpoint debería correr
> /refine. El auto-refine escribe estado local por defecto, así que aprobá cuando la
> trayectoria contenga evidencia útil para los próximos turnos de esta sesión. Rechazá ruido
> de una sola vez, hipótesis sin respaldo y salidas transitorias de herramientas. Pedí
> refinamiento global solo para lecciones durables entre sesiones o lecciones de proyecto
> explícitamente calificadas. Devolvé solo JSON: `{shouldRefine, rationale, instructions}`."*

Parámetros: 4.096 tokens de salida, **40.000 caracteres** de conversación (la mitad que el
refiner), y el mismo modelo de la sesión —no el auxiliar; el `auxiliaryModel` está reservado
para compactación y resúmenes de rama—. `instructions` es lo que el reviewer puede agregar
al plan: es la única forma en que el gate influye en el contenido, no solo en el sí/no. Un
review que rechaza devuelve `Ok(None)`, no un error: "no" es un resultado válido.

### 4.4 La planificación: qué ve el refinador

El prompt del planificador es el corazón del sistema. Reglas que fija (resumidas del texto
literal):

- los cinco tipos y para qué sirve cada uno, con la forma de invocación exacta
  (`await refine...`, `await rlm.spawn(...)`, `await rlm.factory.run('<id>')`) — sin eso el
  modelo inventa wrappers que no existen (`run_subagent(...)`, `call_skill(...)`);
- **el system prompt base es inmutable y no se reescribe**; las entradas `prompt` son notas
  suplementarias;
- política de scope (local por defecto, qué merece global, prefijos display-only);
- *"preferí edits chicos y respaldados por evidencia. Si refinamientos previos causaron
  problemas, hacé rollback o reemplazá las entradas falladas. Nunca edites archivos de
  código directamente."*;
- salida: **solo JSON** con `summary`, `rationale`, `expectedOutcome`, `edits[]`.

Contexto que recibe, en cuatro bloques con etiquetas XML:

| Bloque | Tope | Detalle |
|---|---|---|
| `<current_harness_state>` | 40 entradas por tipo, 240 chars de contenido | `overview_for_prompt`, con `ref=`/`args=` para skills |
| `<refinement_history>` | 20 refinamientos | id, resumen, `applied/failed <acción> <tipo>:<id>`, `rollbackOf`, expected outcome |
| `<conversation>` | 80.000 chars | serializada y **recortada por el frente** (se conserva el final) |
| `<scope_policy>` | fijo | la instrucción de scope de esta corrida |

Si el request no entra en la ventana del modelo, se recorta por búsqueda binaria desde el
principio y se clampa el presupuesto de salida (1.024 tokens de overhead reservados). Nunca
se corta el final de la conversación: lo último es lo que justifica el edit.

El parseo es tolerante en la forma y estricto en el fondo: busca el objeto JSON dentro del
texto (los modelos a veces lo envuelven en prosa o en fences), y si detecta que el JSON quedó
incompleto devuelve un mensaje que dice exactamente eso —*"el modelo cortó antes de cerrar
el objeto; probablemente se agotó el presupuesto de salida"*— en vez de un `unexpected EOF`.
Los ids se inventan como `refine_<17 dígitos del ISO>`, así que son ordenables y legibles.

### 4.5 Validación: las reglas, ninguna negociable

| Regla | Mensaje si falla |
|---|---|
| `action` ∈ {create,update,delete} | `unsupported action` |
| `kind` ∈ los cinco | `unsupported kind` |
| `prompt` con id `base_system_prompt` | `base system prompt is not editable` |
| update/delete sin `id` | `<action> requires id` |
| create/update sin `title` o `content` | `<action> requires title and content` |
| skill sin `arguments` | `<action> skill requires arguments` |
| skill sin `reference` | `<action> skill requires python reference` |
| `reference.type != "python"` | `<action> skill reference.type must be python` |
| skill sin `import` / sin `callable|call_pattern` | `... requires python import` / `... requires callable or call_pattern` |
| factory con `dag` **y** `machine` | `pass either dag or machine form, not both` |
| factory sin objeto en `arguments` | `factory entry requires a dag or machine object` |

Dos cosas finas acá:

- El `base_system_prompt` se chequea **también contra el id calculado**. Si el modelo crea
  una entrada con título "Base System Prompt" y sin id, el slug da `base_system_prompt` y se
  rechaza igual. Es la única forma de que la inmutabilidad sea real y no una convención.
- Las skills exigen `arguments` explícito, y `{}` es válido **solo** si la función no recibe
  nada. El objetivo es que una skill sin contrato no se pueda guardar, porque una skill sin
  argumentos declarados es una skill que el próximo turno va a invocar mal.
- La validación de factory es **estructural**: el validador de semántica completo
  (`rlm.factory`) corre en el kernel al escribir. El comentario del código lo dice con todas
  las letras: *"no reimplementar acá"*.

### 4.6 Apply: por entrada, no por lote

`apply_refinement_proposal` recorre los edits y produce, para cada uno, una fila con
`applied`, `error`, `before` y `after`. Los edits **no son atómicos**: si el tercero falla,
los otros dos quedan aplicados y el resultado lo dice. Es deliberado — abortar el lote por
un edit malo castigaría los buenos — pero significa que el estado final es "lo que se pudo",
y por eso el resultado se le muestra al modelo entrada por entrada.

Tres chequeos que evitan las carreras típicas de un LLM lento:

1. **`baseline_state`**: el state del scope target se captura **antes** de planning. Al
   aplicar, si una entrada cambió respecto de ese baseline y esta propuesta no la tocó
   antes, el edit se rechaza con `entry changed during refinement planning`. La llamada al
   LLM puede tardar 30 s; en ese tiempo el usuario puede haber editado a mano.
2. **Re-lectura del store** entre plan y apply (comment: *"los callers releen el archivo
   antes de aplicar porque la llamada al LLM puede tardar muchos segundos"*).
3. **`factory_enabled` se lee después del request**, nunca antes: el opt-in se resuelve en
   el momento de aplicar, así un `/factory off` durante el planning se respeta. Un create o
   update de factory con el opt-in apagado se rechaza con el mismo mensaje que el kernel
   (`the factory is disabled; run /factory on to enable it`). Un **delete** sí pasa: limpiar
   no es autorizar.

Y la propiedad anti-duplicado que sale gratis del slug: un create sin id deriva el id de un
slug del título (minúsculas, alfanumérico, `_`, máximo 80 chars, fallback al tipo). Crear
dos veces la misma lección con el mismo título no duplica: da `entry already exists`.

### 4.7 Qué vuelve al modelo (y qué no)

Después de aplicar se escriben **tres filas** en la sesión, con propósitos distintos:

| Fila | Tipo | Para quién | Contenido |
|---|---|---|---|
| `prime-agent.refinement` | **entrada estructurada** (no es un mensaje de chat) | la UI | el `RefinementResult` completo, con `applied_edits` |
| `refinement_outcome` | mensaje, `display: true` | el usuario | resultado con `applied_edits` |
| `refinement_notice` | mensaje, `display: false` | **el modelo** | solo si hubo edits aplicados: resumen + una línea por edit con `create memory [local:id] título: contenido` (140 chars) |

El notice es la pieza que cierra el lazo: en el turno siguiente el modelo *ve* lo que acaba
de aprender, con el id real para poder actualizarlo o deshacerlo. Sin ese eco, el refinador
escribe estado que el autor no sabe que existe.

### 4.8 Rollback: el inverso exacto, no un "deshacer" genérico

`rollback_proposal(target)` recorre los edits aplicados **en orden reverso** y construye el
plan contrario:

- si el edit tenía `before` → `update` con los campos del `before` (o `create` si el edit
  había borrado),
- si no tenía `before` pero sí `after` (o sea, fue un create) → `delete`.

El resultado es un refine nuevo (`refine_<ts>`) con `rollback_of` apuntando al original y el
scope **inferido del target**, no del flag de la corrida. Es decir: deshacer un refine local
no requiere que el usuario se acuerde de pasar `--local`. Y como todo pasa por el mismo
apply, un rollback se valida y se audita igual que cualquier otro refine.

---

## 5. Cómo el modelo ve el estado: el digest

La decisión de arquitectura más importante de todo el sistema: **el harness no se inyecta en
el system prompt**. Se inyecta como un mensaje custom (`[harness-digest] … </harness_state>`)
en los **límites fríos** del contexto: arranque de sesión, reanudación, y cabeza de
compactación. Tres consecuencias:

1. El prefijo del system prompt queda **byte-idéntico** entre turnos, así que el caché de
   prompt del proveedor sigue sirviendo. Reescribirlo en cada refine tiraría el caché y
   costaría caro en tokens de entrada.
2. El digest es **reemplazable**: en el contexto sobrevive solo el más nuevo (la compactación
   lo arrastra dentro del summary). No se acumulan versiones viejas del estado.
3. Como es un mensaje y no configuración, se puede renderizar distinto según la sesión
   (¿hay REPL? ¿hay shell?) sin tocar nada persistido.

### 5.1 Fingerprint: no re-inyectar lo mismo

`harness_digest_fingerprint` es un SHA-256 sobre el material canónico del estado (entradas
ordenadas por `scope\0kind\0id`, con `reference`/`arguments` solo para skills, más los
refinamientos en su orden de almacenamiento, más las flags de render y una
`FINGERPRINT_VERSION`). Si el fingerprint no cambió, el boundary frío **no re-emite** el
digest.

El detalle que hace que esto funcione: **los términos de la consulta quedan fuera del
fingerprint**, aunque sí afectan el render. Si entraran, cada turno tendría un digest
"distinto" por relevancia y el fingerprint no serviría para nada. Es la clase de error que
convierte una optimización en un costo.

### 5.2 Ranking: tf-idf chico, pero tf-idf

El digest no entra entero: entra lo relevante a la tarea actual.

- **Términos**: palabras de ≥4 caracteres, en minúsculas; CJK como bigramas solapados (para
  que `登录` matchee `登录故障`). Tope de 48 términos.
- **Pesos**: el objetivo activo del goal pesa **3.0**; los últimos 4 textos, 2.0 → 1.5 → 1.0
  → 1.0 (decreciente, con piso).
- **IDF por tipo**: `ln(1 + N/df)` sobre las entradas **de ese tipo** (no de todas), porque
  compiten por los mismos 3 slots. Un término en todas las entradas pesa `ln 2`; uno en una
  sola pesa `ln(1+N)`.
- **Score**: `peso × idf × (1 + 0.5×(campos−1))`, donde `campos` cuenta coincidencias en
  título / contenido / (path+id). Premia cobertura, no repetición: repetir la palabra diez
  veces no suma nada.

Topes del bloque: **3 entradas por tipo**, 10 refinamientos, 140 caracteres por contenido.
El resto se resume como `+N more <kind> entries`. Es chico a propósito: el digest compite
con el trabajo real, y la línea `(entries ranked by relevance to the current task; see
harness.search)` le dice al modelo qué hacer si necesita el resto — `rlm.harness.search()`
usa el mismo scoring en el kernel.

---

## 6. Por qué esto es "mejora" y no "deriva"

Cinco candados, cada uno contra un modo de falla concreto:

| Candado | Contra qué |
|---|---|
| `base_system_prompt` inmutable (verificado por id **y** por slug) | que un refine reescriba la identidad del agente y no haya vuelta atrás |
| local por defecto, y global read-only durante un refine local | que una sesión contamine el resto |
| todo versionado con `before`/`after` + rollback por id | que una entrada mala sea irreversible |
| gates numéricos antes del LLM + review gate | que el auto-refine gaste tokens en ruido |
| apply por entrada con baseline check | que un plan viejo pise un edit humano |

Lo que **no** tiene candado, dicho sin maquillaje:

1. **El juez es el autor.** El review gate es el mismo modelo de la sesión, con otro prompt.
   Comparte sesgos con el planificador. Un gate de otro modelo (o de uno más chico con
   instrucciones opuestas) sería una señal más independiente.
2. **Se valida la forma, no la verdad.** Que un edit sea un `memory` bien formado no dice
   nada sobre si el hecho es cierto. El prompt pide "evidencia de la trayectoria", pero nadie
   lo verifica. La defensa real es que sea local y reversible.
3. **El cooldown y los contadores son en memoria.** Se pierden al reiniciar.
4. **El digest muestra 3 por tipo.** Con un query pobre (conversación corta o genérica), una
   entrada útil puede quedar fuera de la ventana. El escape es `harness.search`, y depende
   de que el modelo se acuerde.
5. **El lote no es atómico.** Un edit que falla no revierte los otros; el resultado lo
   reporta, pero el estado final es parcial.
6. **El historial append-only crece sin poda.** `refinement_history.jsonl` no tiene retención
   en lo que leí; el prompt solo toma los últimos 20.
7. **`factory` está apagado por defecto** y su validador semántico vive fuera del planner.
   Eso es correcto, pero significa que un edit de factory puede guardarse y fallar recién al
   correr.

---

## 7. Las otras tres patas (que el README mete en la misma lista)

### 7.1 `goal` — continuación, no memoria

Un objetivo persistente que el harness **re-promptea** hasta que el modelo llame a
`goal.complete()`. Estado: `objective`, `status`, `token_budget`, `tokens_used`,
`time_used_seconds`. Reglas que importan:

- el modelo solo puede `get`, `create` y `complete`; **pausar/reanudar/limpiar es del
  usuario**. El agente no puede darse permiso para seguir gastando.
- `create` falla si hay un goal pendiente; un goal `completed`/`errored` sí se reemplaza.
- el prompt de la skill es explícito: *"no infieras goals de tareas comunes"* y *"no llames a
  complete solo porque se está acabando el presupuesto"*. Sin esas dos frases, todo goal
  termina autocompletándose.
- el conteo de tokens se reancla en los rebuilds de rama: en el mismo timeline los contadores
  se clampean al máximo y nunca retroceden; en un salto de rama se recarga el goal de esa
  rama.

Es automejora en el sentido de *persistencia de intención*, no de conocimiento.

### 7.2 `compact` — autonomía de contexto

`await compact.status()` / `compact.run(instructions)` programan la compactación para el fin
del turno, exactamente como `refine`. Dos detalles que valen: el **kernel de Python
sobrevive** a la compactación (variables, imports y helpers siguen vivos — el estado no está
solo en el texto) y `percent` es `None` justo después de compactar hasta la próxima
respuesta, porque el uso real todavía no se midió. El `instructions` es lo que evita que el
resumen se coma lo que falta: *"conservá los nombres de los tests que fallan y el checklist
de migración"*.

### 7.3 `factory` y `skill-creator` — procedimientos, no hechos

`factory` guarda una máquina de estados de subagentes (estados con spec, transiciones con
guardas, joins, foreach, presupuestos) y la corre en una tarea de fondo del kernel: `run`
vuelve inmediato con un `run_id`, `status/watch/stop/resume` la manejan. Opt-in explícito
(`/factory on`), porque un workflow que spawnea agentes puede gastar mucho. Una spec inválida
no se guarda nunca.

`skill-creator` es la otra puerta de la automejora: en vez de un edit de harness, el modelo
**escribe un paquete Python** con su `SKILL.md`, lo instala y lo importa. Es más potente y
más peligroso que una entrada `skill` (código arbitrario vs. contrato declarativo), y por eso
vive en el filesystem del usuario y no en el state del harness.

---

## 8. Plan de aterrizaje en nexus-v3

El orden no es negociable: primero el almacén seguro, después el digest, y recién al final el
LLM. Si se invierte, el refinador escribe sobre un almacén que puede corromperse y nadie lo
nota hasta que ya perdió memoria.

### Fase 0 — el almacén (sin LLM)

`src/core/harness_state.py`: un solo archivo `data/harness/state.json` (o
`<proyecto>/.nexus/harness.json` para el scope proyecto), con el schema de §1 y las cinco
clases de entrada. Reutilizar lo que ya existe en vez de reinventarlo:

- **lock**: `src/core/` ya tiene el patrón de `scope` y de escritura atómica en
  `checkpoints.py`; el lock de directorio con owner es ~80 líneas y es lo único que hay que
  escribir de cero.
- **versionado**: los `checkpoints.snapshot()` ya dan el undo de archivos; acá hace falta el
  undo **por entrada**, que es el `before`/`after`.
- **scope**: `scope.check_path` ya decide qué rutas son del proyecto; el harness hereda esa
  decisión (proyecto vs global) en vez de inventar una segunda noción de "afuera".

Tool `harness` con `list|get|create|update|delete|search|history|rollback`, y un
`AGENTS.md`-style block en el system prompt… o mejor: el digest de la Fase 1, desde el día
uno, aunque sea sin ranking. Tests obligatorios: escritura concurrente (dos hilos × N
rondas), archivo corrupto, lock huérfano con pid muerto, y `base_system_prompt` rechazado
por id y por slug.

### Fase 1 — el digest

`src/core/harness_digest.py`: render por tipo con topes (3/10/140), ranking tf-idf con pesos
(goal 3.0, últimos 4 textos), fingerprint sha256 sin los términos de consulta, y emisión solo
si el fingerprint cambió respecto del último emitido en la sesión.

nexus-v3 ya tiene `brain/knowledge_graph.db` con FTS5, pero **no** conviene usarlo acá: el
harness es por sesión y chico (decenas de entradas), y una consulta SQL por turno para rankear
20 filas es peor que un `sorted()` en Python. El grafo sirve para el conocimiento del
proyecto; el harness para el estado del agente. Son cosas distintas y mezclarlas fue el error
de v2.

### Fase 2 — `/refine` manual

`src/core/refine.py`, reusando `provider_router` para la llamada:

1. `plan(trajectory, state, history, instructions, scope)` → JSON validado (las 9 reglas de
   §4.5, con el mismo texto de error).
2. La **propuesta se muestra antes de aplicar**: es el equivalente del
   `require_confirm_mutating` que ya existe en el sandbox. Nada de auto-apply en esta fase.
3. `apply(plan, baseline)` por entrada, con `entry changed during planning` y las filas
   `applied/error/before/after`.
4. `history` append-only en `data/harness/history.jsonl` + notice como mensaje de sistema
   (marcado `display: false` en la UI).

Comando `/refine` en `slash_commands.py` (el registry ya soporta `origin`), y `/refine undo
<id>` para el rollback. Config:

```yaml
refine:
  enabled: false          # nada se auto-aplica con esto en false
  scope: project          # project | global
  after_compaction: false # el trigger automático, apagado por defecto
  turn_interval: 25
  cooldown_ms: 1200000
  max_edits: 8            # tope duro por corrida (no lo tiene prime-agent)
  model: ""               # vacío = el de la sesión
```

`max_edits` es un agregado propio: prime-agent no limita cuántos edits trae un plan; un
modelo entusiasta puede devolver 40. Ocho es suficiente para una lección y hace visible
cuando el modelo está inflando.

### Fase 3 — auto-refine tras compactación (con evals)

Solo después de tener la suite de evaluación. El trigger es barato de copiar (armar en
`compaction`, consumir en checkpoint/dispose, cooldown, branch version), pero encenderlo sin
medición es cómo se degrada un harness en silencio. La suite mínima: 10 trayectorias
etiquetadas a mano con las lecciones que **deberían** salir y las que **no** (un fix puntual,
un error de tool transitorio). Se corre el planner contra cada una y se mide precisión/recall
de edits. Umbral para encender: 0 falsos positivos en las 10.

### Qué NO copiar

| No | Por qué |
|---|---|
| el REPL como única interfaz | nexus ya tiene tools tipadas; el REPL es un extra, no el contrato |
| `factory` (máquina de estados de subagentes) | `dag_coordinator` cubre el caso; el costo de mantener un validador de máquinas no se paga |
| los nombres `RLM` / "continual harness" | son de su paper; en nexus es "memoria del agente" y "harness state" |
| el kernel de Python con venvs por tool | acá el Python del proyecto ya es fijo (`.venv-ft314`) |
| el review gate con el mismo modelo | si se copia, el gate debería usar otro modelo o reglas deterministas primero |

### Riesgos y mitigación

| Riesgo | Mitigación en el plan |
|---|---|
| el refinador inventa hechos | scope proyecto + confirmación manual + rollback por id |
| se degrada el prompt con el tiempo | tope de 3 entradas inyectadas por tipo y `version` visible; una entrada que crece demasiado se parte o se borra |
| dos procesos escriben el harness (server + UI) | el lock de directorio de §3, no un `threading.Lock` |
| el auto-refine gasta tokens sin control | `after_compaction: false` + `max_edits` + cooldown |
| el usuario no entiende qué aprendió | el notice es visible en la UI como panel "Aprendizajes", no como mensaje suelto |

---

## 9. Evidencia

| Pieza | Archivo | Líneas |
|---|---|---|
| tipos, acciones, state, merge `local:`, historial, notice | `crates/pa-core/src/refinement/mod.rs` | 871 |
| prompt del refinador, validación, apply, rollback, request | `crates/pa-core/src/refinement/planner.rs` | 991 |
| plan/review/apply, prompt del gate, seam del modelo | `crates/pa-core/src/refinement/executor.rs` | 615 |
| ranking tf-idf, render del digest, fingerprint | `crates/pa-core/src/refinement/ranking.rs` | 886 |
| gates (`turn_interval` 25, `cooldown_ms` 20 min), fuentes, filas de sesión | `crates/pa-core/src/session_engine/refine.rs` | 1227 |
| máquina del trigger, superficies, branch version | `crates/pa-core/src/session_engine/auto_refine_trigger.rs` | 469 |
| master switch y filtrado de filas | `crates/pa-core/src/session_engine/refinement_gate.rs` | 211 |
| entrega del digest en boundaries fríos | `crates/pa-core/src/session_engine/harness_digest.rs` | 586 |
| escritura del kernel: lock, mtime sync, atomic, search | `prime-agent-runtime/src/rlm/harness.py` | 1263 |
| skills `refine` / `goal` / `compact` / `factory` | `skills/*/SKILL.md` | 2.1k / 2.2k / 1.8k / 3.3k |
| tesis del producto | `README.md` (*"A Self-Improving RLM Harness"*, continual harness) | — |

Nota metodológica: nada de esto sale del README ni de la documentación del repo. El README
dice *qué* promete; los archivos de arriba dicen *cómo* y *con qué garantías*. Donde el código
admite un límite (§6), está escrito el límite.




