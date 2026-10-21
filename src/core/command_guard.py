"""Guard de comandos para plan mode (read_only).

Port de `sdk/packages/core/src/extensions/tools/command-guard.ts` de Cline
(commit e9dafbb, Apache-2.0). Cline mantiene `run_commands` disponible en plan
mode pero rechaza los comandos que modifican archivos; nexus-v3 bloqueaba
`exec` entero en `read_only`, lo que impedia investigar con git/grep/ls.

Este guard NO es un interprete de shell: enmascara texto que es dato (quotes,
heredocs, escapes, comentarios), parte el comando por separadores y compara la
palabra inicial de cada tramo contra una blacklist. No detecta mutaciones
arbitrarias (`python -c "open(..., 'w')"`), y por eso se combina con
`_exec_jail` (que en read_only sigue prohibiendo encadenar/redirigir) y con el
scope de paths. Es defensa en profundidad, no una jaula de SO.
"""
from __future__ import annotations

import os
import re
from typing import Any

BLOCKED_COMMANDS = frozenset({
    # POSIX
    "rm", "rmdir", "unlink", "mv", "cp", "dd", "touch", "mkdir", "ln", "link",
    "chmod", "chown", "chgrp", "truncate", "shred", "install", "patch", "rsync",
    "mkfifo", "mknod",
    # Windows / PowerShell
    "del", "erase", "move", "ren", "rename", "md", "rd", "mklink", "copy",
    "xcopy", "robocopy", "new-item", "ni", "remove-item", "move-item",
    "copy-item", "rename-item", "set-content", "add-content", "clear-content",
    "out-file",
    # Aliases inequivocos de esos cmdlets
    "mi", "ri", "cpi", "rni", "ac", "clc",
})

GIT_SUBCOMMANDS = frozenset({
    "add", "am", "apply", "checkout", "cherry-pick", "clean", "clone", "commit",
    "init", "merge", "mv", "pull", "push", "rebase", "reset", "restore",
    "revert", "rm", "stash", "submodule", "switch", "worktree",
})
NODE_PM_SUBCOMMANDS = frozenset({
    "install", "i", "ci", "add", "remove", "rm", "uninstall", "unlink", "link",
    "update", "up", "upgrade", "dedupe", "prune", "publish",
})
PIP_SUBCOMMANDS = frozenset({"install", "uninstall", "add", "remove"})
CARGO_SUBCOMMANDS = frozenset({
    "add", "remove", "rm", "install", "uninstall", "update", "new", "init",
    "publish",
})
GEM_SUBCOMMANDS = frozenset({"install", "uninstall", "update", "cleanup"})
COMPOSER_SUBCOMMANDS = frozenset({"install", "require", "remove", "update"})
GO_SUBCOMMANDS = frozenset({"install", "get"})
DOTNET_SUBCOMMANDS = frozenset({"add", "remove"})
WINGET_SUBCOMMANDS = frozenset({"install", "uninstall", "upgrade"})
NUGET_SUBCOMMANDS = frozenset({"install", "update", "delete", "push"})
SYSTEM_PM_SUBCOMMANDS = frozenset({
    "install", "reinstall", "remove", "purge", "uninstall", "upgrade",
    "autoremove", "add", "del",
})

BLOCKED_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": GIT_SUBCOMMANDS,
    "npm": NODE_PM_SUBCOMMANDS,
    "pnpm": NODE_PM_SUBCOMMANDS,
    "yarn": NODE_PM_SUBCOMMANDS,
    "bun": NODE_PM_SUBCOMMANDS,
    "pip": PIP_SUBCOMMANDS,
    "pip3": PIP_SUBCOMMANDS,
    "pipx": PIP_SUBCOMMANDS,
    "uv": PIP_SUBCOMMANDS,
    "cargo": CARGO_SUBCOMMANDS,
    "apt": SYSTEM_PM_SUBCOMMANDS,
    "apt-get": SYSTEM_PM_SUBCOMMANDS,
    "dnf": SYSTEM_PM_SUBCOMMANDS,
    "yum": SYSTEM_PM_SUBCOMMANDS,
    "apk": SYSTEM_PM_SUBCOMMANDS,
    "brew": SYSTEM_PM_SUBCOMMANDS,
    "snap": SYSTEM_PM_SUBCOMMANDS,
    "gem": GEM_SUBCOMMANDS,
    "composer": COMPOSER_SUBCOMMANDS,
    "go": GO_SUBCOMMANDS,
    "dotnet": DOTNET_SUBCOMMANDS,
    "winget": WINGET_SUBCOMMANDS,
    "nuget": NUGET_SUBCOMMANDS,
}

READ_ONLY_GIT_FORMS: dict[str, frozenset[str]] = {
    "stash": frozenset({"list", "show"}),
    "worktree": frozenset({"list"}),
    "submodule": frozenset({"status", "summary"}),
}

WRAPPERS = frozenset({
    "sudo", "doas", "env", "command", "builtin", "nohup", "time", "nice",
    "stdbuf", "timeout", "xargs",
})

RESERVED_WORDS = frozenset({
    "if", "then", "else", "elif", "fi", "while", "until", "do", "done", "for",
    "case", "esac", "!", "{", "}",
})

ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
IN_PLACE_FLAG = re.compile(r"^-[A-Za-z]*i(\..*)?$")
_HEREDOC = re.compile(
    r'(?<![<])<<-?\s*(?:"([^"]+)"|\'([^\']+)\'|([A-Za-z0-9_./-]+))'
)
_REDIRECT = re.compile(r"(?:^|[^<>])(?:>{1,2}|&>{1,2})\s*([^\s;&|<>()]+)")
_SPECIAL_IN_QUOTES = re.compile(r"[\s<>|;&$()`#\\]")


def is_allowed_redirect_target(raw_target: str) -> bool:
    """Destinos que no persisten datos o viven en tmp."""
    target = raw_target.replace('"', "").replace("'", "")
    if re.match(r"^(/dev/(null|stdout|stderr|tty)|nul:?)$", target, re.I):
        return True
    if ".." in target:
        return False
    if target.startswith(("/tmp/", "/var/tmp/", "$TMPDIR", "${TMPDIR")):
        return True
    return bool(
        re.match(r"^%te?mp%", target, re.I)
        or re.match(r"^\$env:te?mp", target, re.I)
    )


def normalize_command_name(word: str) -> str:
    base = re.sub(r'["\']', "", word).replace("\\", "/").split("/")[-1] or word
    return re.sub(r"\.exe$", "", base.lower())


def mask_non_command_text(command: str) -> str:
    """Reemplaza por `_` el texto que es dato y no comando."""
    lines = command.split("\n")
    kept: list[str] = []
    heredoc_delimiter: str | None = None
    for line in lines:
        if heredoc_delimiter is not None:
            if line.lstrip("\t") == heredoc_delimiter:
                heredoc_delimiter = None
            continue
        kept.append(line)
        match = _HEREDOC.search(line)
        if match:
            heredoc_delimiter = match.group(1) or match.group(2) or match.group(3)

    masked = "\n".join(kept)
    masked = re.sub(r"\$\(\(.*?\)\)", "_", masked, flags=re.S)
    masked = re.sub(r"\\[\s<>|;&$()`#\\'\"!]", "_", masked)
    masked = re.sub(
        r'"[^"]*"|\'[^\']*\'',
        lambda m: _SPECIAL_IN_QUOTES.sub("_", m.group(0)),
        masked,
    )
    masked = re.sub(r"(?:^|\s)#.*$", " ", masked, flags=re.M)
    return masked


def _skip_prefix_tokens(tokens: list[str]) -> int:
    index = 0
    while index < len(tokens) and (
        ENV_ASSIGNMENT.match(tokens[index]) or tokens[index] in RESERVED_WORDS
    ):
        index += 1
    while index < len(tokens) and normalize_command_name(tokens[index]) in WRAPPERS:
        index += 1
        while index < len(tokens) and (
            tokens[index].startswith("-")
            or tokens[index] == "{}"
            or ENV_ASSIGNMENT.match(tokens[index])
            or re.match(r"^\d+[smhd]?$", tokens[index])
        ):
            index += 1
    return index


def check_tokens(tokens: list[str]) -> str | None:
    """Devuelve una descripcion corta del primer constructo mutante, o None."""
    index = _skip_prefix_tokens(tokens)
    if index >= len(tokens):
        return None

    name = normalize_command_name(tokens[index])
    args = tokens[index + 1:]

    # Shell anidado: `cmd /c <cmd>` y `bash -c <cmd>` sin quotes presentes en
    # el token (las quotes ya se enmascararon). No cubre payloads quoted.
    if name in ("cmd", "powershell", "pwsh", "bash", "sh", "zsh", "dash"):
        for i, arg in enumerate(args):
            if arg in ("/c", "/C", "-c", "-Command", "-command", "/Command"):
                nested = args[i + 1:]
                return check_tokens(nested) if nested else None
        return None

    if name == "tee":
        file_args = [a for a in args if not a.startswith("-")]
        if any(not is_allowed_redirect_target(a) for a in file_args):
            return "`tee`"
        return None

    if name in BLOCKED_COMMANDS:
        return f"`{name}`"

    subcommands = BLOCKED_SUBCOMMANDS.get(name)
    if subcommands is not None:
        if name == "git" and any(a in ("--help", "-h") for a in args):
            return None
        cursor = 0
        while cursor < len(args) and args[cursor].startswith("-"):
            cursor += 2 if name == "git" and re.match(r"^-[cC]$", args[cursor]) else 1
        subcommand = args[cursor].lower() if cursor < len(args) else None
        if subcommand and subcommand in subcommands:
            nxt = args[cursor + 1].lower() if cursor + 1 < len(args) else None
            if name == "git" and nxt is not None and nxt in READ_ONLY_GIT_FORMS.get(subcommand, frozenset()):
                return None
            return f"`{name} {subcommand}`"
        if name == "yarn" and not args:
            return "`yarn` (install)"
        return None

    if re.match(r"^python[\d.]*$", name):
        try:
            module_index = args.index("-m")
        except ValueError:
            module_index = -1
        if module_index != -1 and module_index + 1 < len(args):
            module = args[module_index + 1].lower()
            if module in ("pip", "pip3"):
                subcommand = next(
                    (a.lower() for a in args[module_index + 2:] if not a.startswith("-")),
                    None,
                )
                if subcommand and subcommand in PIP_SUBCOMMANDS:
                    return f"`pip {subcommand}`"
        return None

    if name in ("sed", "gsed") and any(
        IN_PLACE_FLAG.match(a) or a.startswith("--in-place") for a in args
    ):
        return "`sed -i` (in-place edit)"
    if name == "perl" and any(IN_PLACE_FLAG.match(a) for a in args):
        return "`perl -i` (in-place edit)"
    if re.match(r"^[gnm]?awk$", name):
        uses_inplace = False
        for i, arg in enumerate(args):
            if (arg in ("-i", "--include")) and i + 1 < len(args) and args[i + 1].startswith("inplace"):
                uses_inplace = True
            elif arg.startswith("-i") and arg[2:].startswith("inplace"):
                uses_inplace = True
            elif arg.startswith("--include=inplace"):
                uses_inplace = True
        if uses_inplace:
            return "`awk -i inplace` (in-place edit)"
        return None
    if name == "sort" and any(a.startswith("-o") or a.startswith("--output") for a in args):
        return "`sort -o` (writes a file)"
    if name == "curl":
        writes = any(
            (not a.startswith("--") and re.match(r"^-[A-Za-z]*[oO]", a))
            or a.startswith("--output")
            or a.startswith("--remote-name")
            for a in args
        )
        return "`curl -o` (writes a file)" if writes else None
    if name == "wget":
        read_only = any(
            a == "--spider"
            or re.match(r"^-[A-Za-z]*O-$", a)
            or a == "--output-document=-"
            or (a == "-O" and i + 1 < len(args) and args[i + 1] == "-")
            for i, a in enumerate(args)
        )
        return None if read_only else "`wget` (downloads files)"
    if name == "find":
        if "-delete" in args:
            return "`find -delete`"
        for i, arg in enumerate(args):
            if arg not in ("-exec", "-execdir", "-ok", "-okdir"):
                continue
            tail: list[str] = []
            cursor = i + 1
            while cursor < len(args) and args[cursor] not in (";", "+"):
                tail.append(args[cursor])
                cursor += 1
            nested = check_tokens(tail)
            if nested:
                return nested
        return None

    return None


def find_file_editing_command(command: str | dict[str, Any]) -> str | None:
    """Inspecciona un comando y devuelve el primer constructo mutante."""
    if isinstance(command, dict):
        argv = [str(command.get("command") or "")]
        argv.extend(str(a) for a in (command.get("args") or []))
        return check_tokens([a for a in argv if a])

    masked = mask_non_command_text(str(command or ""))

    for match in _REDIRECT.finditer(masked):
        target = match.group(1)
        if not is_allowed_redirect_target(target):
            return f"output redirection (`> {target}`)"

    for segment in re.split(r"[\n;|&()`]+", masked):
        tokens = [t for t in segment.strip().split() if t]
        blocked = check_tokens(tokens)
        if blocked:
            return blocked

    return None


def format_plan_mode_blocked_error(reason: str) -> str:
    return (
        f"Comando no ejecutado: {reason} puede modificar archivos, y las "
        "modificaciones estan bloqueadas en plan mode. Estas en PLAN MODE: "
        "explora, analiza y presenta un plan; no hagas cambios. Usa comandos "
        "de solo lectura para inspeccionar el proyecto (redirigir a /tmp o "
        "%TEMP% en Windows esta permitido) y, si el cambio es parte de la "
        "tarea, incluyelo en el plan para ejecutarlo tras la aprobacion."
    )


def command_uses_shell_metacharacters(command: str) -> bool:
    """True si el comando encadena, redirige o sustituye (para el jail)."""
    return bool(re.search(r"[;&|`<>]|\$\(|\$\{|\r|\n", command or ""))
