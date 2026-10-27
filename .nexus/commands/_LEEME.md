# Comandos personalizados

Cada archivo `.md` de esta carpeta es un comando de barra: el nombre del archivo
es el nombre del comando. `revisar.md` se invoca con `/revisar`.

Los archivos que empiezan con `_` o `.` se ignoran (sirven para documentación).

## Formato

```markdown
---
description: Qué hace (aparece en la paleta de comandos)
argument-hint: "<ruta> [otra cosa]"
agent: code          # opcional: cambia el ayudante
model: qwen2.5-coder:7b   # opcional: fuerza el modelo
alias: [rv]          # opcional
---
Cuerpo del prompt. Acá va lo que se le manda al modelo.
```

El frontmatter es opcional; sin él, la descripción sale de la primera línea.

## Sustituciones en el cuerpo

| Texto | Se reemplaza por |
|---|---|
| `$ARGUMENTS` | todos los argumentos, tal cual |
| `$1` … `$9` | el argumento en esa posición (vacío si falta) |
| `@ruta/al/archivo` | el archivo como bloque de código (máx. 24.000 caracteres) |

`@ruta` solo puede leer **dentro del proyecto**: una ruta de afuera no se incluye
y el comando avisa por qué.

## Dónde se buscan

1. `<proyecto>/.nexus/commands/`
2. `<proyecto>/.nexus/command/`
3. `~/.nexus/commands/`
4. Los que agregue `commands.dirs` en `config/config.yaml`

`NEXUS_COMMANDS_DIR` (separado por `;`) reemplaza toda la lista.

Los comandos del disco **no pisan** los builtins: si un archivo se llama `help.md`,
se ignora y queda el `/help` del sistema.
