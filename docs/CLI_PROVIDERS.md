# CLIs de agentes como proveedores

`src/providers/cli_provider.py` envuelve CLIs que ya son agentes completos
(`claude`, `codex`, `opencode`) y los expone al router como un proveedor más.

No son modelos: son procesos con su propio agente, sus tools y su cuenta. Acá se
usan como **consultores** (una pregunta, una respuesta), por eso:

- `supports_tools = False` → el router los saltea cuando el turno necesita
  herramientas (no se les puede dar `write_file` ni esperar tool calls).
- `supports_vision = False`.
- Cada turno manda **un prompt único**: system + último pedido, y un resumen
  corto de los turnos previos. No se replica la conversación entera.

## Estado en esta PC (2026-10-25)

| CLI | Binario | Versión | Estado |
|---|---|---|---|
| `opencode` | `npm/.../opencode-ai/bin/opencode.exe` | 1.19.3 | **funciona**: responde vía `opencode run --format json` |
| `claude` | `~/.local/bin/claude.exe` | 2.1.167 | instalado, **sin credencial válida** (401) |
| `codex` | `npm/.../@openai/codex/bin/codex.js` | 0.160.1 | logueado, pero la cuenta ChatGPT **no habilita** los modelos configurados (`gpt-5.5`, `gpt-5`, `gpt-5-codex`) |

Por eso los tres vienen **apagados** en `config.yaml`. `gemini` y
`cursor-agent` no están instalados.

## Cómo se habilita

```yaml
providers:
  cli:
    opencode:
      enabled: true
      timeout_s: 180
      # model: provider/modelo      # opcional -> se pasa con -m
      # cwd: .                      # default: directorio descartable
```

El provider queda registrado con el id de su modelo:
`opencode` (sin `model`) u `opencode:<model>`. Para elegirlo en un turno:
`{"model": "opencode"}` en `/api/chat`, o `/modelo opencode` en la UI.

## Modos seguros por defecto

Un CLI de agentes trae sus propias tools: si se lo apunta al repo, puede
editarlo. Estos son los frenos que ya aplica el adaptador:

| CLI | Modo | Efecto |
|---|---|---|
| `claude` | `--permission-mode plan` | investiga y responde, no edita |
| `codex` | `-s read-only` | sandbox de solo lectura |
| `opencode` | — | usa las policies de tu propia config de opencode |
| todos | `cwd` descartable | `%TEMP%\nexus-v3-cli\<nombre>`, fuera del repo |

Para que un CLI vea el proyecto hay que pedirlo: `cwd: .`.

## Tres trampas de Windows que ya están resueltas

1. **`claude -p` se colgaba para siempre.** 45 s sin stdout ni stderr, matado a
   mano. La causa no era el CLI: `ANTHROPIC_BASE_URL` apuntaba a
   `127.0.0.1:3456`, un router local que estaba caído. Sin esa variable el mismo
   comando falla en 2,5 s con un 401 explícito. Ahora se sondea el `base_url`
   antes de lanzar y se corta con un mensaje accionable; si el router es
   intencional, se levanta.
2. **`codex` espera por stdin.** Cuando stdin no es una TTY imprime "Reading
   additional input from stdin..." y con un pipe abierto no termina nunca. El
   adaptador siempre usa `stdin=DEVNULL`.
3. **Los shims de npm no son ejecutables Win32.** `codex` y `opencode` se
   instalan como `.cmd`/`.ps1`; lanzarlos directo da "%1 no es una aplicación
   Win32 válida". Y pasarlos por `cmd /c` rompe el prompt en cuanto tiene `%`,
   `&` o comillas. El adaptador lee el shim y resuelve el binario real
   (`node ...\codex.js`, `...\opencode.exe`).

## Errores y enfriamiento

Un CLI que falla (sin login, sin cuota, proxy caído) entra en **enfriamiento
300 s**: `health()` devuelve `False` y el router no lo elige. `health()` no corre
`--version` porque el probe global tiene 2,5 s para todos; para la versión real
está `probe()`.

Los errores suben con el comando, el `exit` y la cola de stderr/stdout, así se
ve el motivo real:

```
claude: Failed to authenticate. API Error: 401 ... | exit=1
codex: {"type":"error","status":400,...} | exit=1
claude: http://127.0.0.1:3456 no responde (TimeoutError). Si es un router/proxy
        local, levantaló; si no, borrá esa variable de entorno.
```

## Cadencia

`opencode run` tarda ~70-100 s por respuesta (arranca el CLI entero cada vez,
sin sesión persistente). No sirve para el loop TDAO con muchas iteraciones; sí
para una segunda opinión puntual o para la mesa redonda.

## Flavor `custom`

Para cualquier otro CLI:

```yaml
providers:
  cli:
    mi-cli:
      enabled: true
      flavor: custom
      command: ["/ruta/al/cli"]      # o el nombre si está en PATH
      argv: ["--ask", "{prompt}", "--model", "{model}"]
```

`argv` acepta `{prompt}`, `{model}` y `{cwd}`; la respuesta es el stdout crudo.
