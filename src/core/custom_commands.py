"""Comandos personalizados — plantillas de prompt invocables con /nombre.

Un archivo ``.md`` define un comando: frontmatter opcional (``description``,
``argument-hint``, ``agent``, ``model``, ``alias``) y un cuerpo que es el
prompt a enviar. Se buscan en ``<proyecto>/.nexus/commands/`` (y ``command/``)
más ``~/.nexus/commands/``.

Sustituciones en el cuerpo:
- ``$ARGUMENTS`` — todos los argumentos, tal cual se escribieron.
- ``$1``…``$9`` — argumento posicional (vacío si falta).
- ``@ruta`` — incluye el archivo como bloque de código acotado.

El comando no ejecuta shell: solo compone texto. Quien decide mandarlo es el
caller (la UI lo envía como turno normal), así el jail y los permisos siguen
siendo los del turno.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from src.core.skill_frontmatter import split_frontmatter

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
POSITIONAL_RE = re.compile(r"\$(\d)")
INCLUDE_RE = re.compile(r"(?<![\w.@])@(?!\w+://)([A-Za-z0-9_.\-/\\:]*\.[A-Za-z0-9]{1,8})")

MAX_FILE_BYTES = 200_000
MAX_INCLUDE_CHARS = 24_000
MAX_COMMANDS = 200

_cache: Dict[str, Tuple[float, int, "CommandSpec"]] = {}


@dataclass
class CommandSpec:
    name: str
    description: str
    body: str
    path: str
    args_hint: str = ""
    agent: str = ""
    model: str = ""
    aliases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "args": self.args_hint,
            "origin": "custom",
        }
        if self.agent:
            out["agent"] = self.agent
        if self.model:
            out["model"] = self.model
        return out


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _config_dirs() -> List[Path]:
    try:
        from src.core.config import load_raw_config

        cfg = load_raw_config() or {}
        raw = (cfg.get("commands") or {}).get("dirs") or []
        return [Path(os.path.expanduser(str(p))) for p in raw if str(p).strip()]
    except Exception:
        return []


def default_dirs() -> List[Path]:
    """Directorios de comandos: proyecto, usuario, y los de config."""
    env = os.environ.get("NEXUS_COMMANDS_DIR")
    if env:
        return [Path(os.path.expanduser(p)) for p in env.split(os.pathsep) if p.strip()]
    root = project_root()
    home = Path(os.path.expanduser("~"))
    ordered = [
        root / ".nexus" / "commands",
        root / ".nexus" / "command",
        home / ".nexus" / "commands",
        *_config_dirs(),
    ]
    out: List[Path] = []
    seen: set = set()
    for d in ordered:
        key = str(d).lower() if os.name == "nt" else str(d)
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _as_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    raw = str(value).strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_frontmatter(fm_text: str) -> Dict[str, object]:
    """YAML si se puede; si no, pares ``clave: valor`` de una línea."""
    try:
        import yaml

        loaded = yaml.safe_load(fm_text)
        if isinstance(loaded, dict):
            return loaded
    except Exception:
        pass
    out: Dict[str, object] = {}
    for line in fm_text.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip().strip("\"'")
    return out


def parse_command(name: str, text: str, path: str) -> Optional[CommandSpec]:
    """`None` si el archivo no es un comando válido (nombre inválido)."""
    clean = name.strip().lower()
    if not NAME_RE.match(clean):
        return None
    fm_text, body, err = split_frontmatter(text)
    if err:
        return None
    meta: Dict[str, object] = _parse_frontmatter(fm_text) if fm_text else {}
    description = str(meta.get("description") or "").strip()
    if not description:
        first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
        description = first.lstrip("#").strip()[:140] or f"comando {clean}"
    return CommandSpec(
        name=str(meta.get("name") or clean).strip().lower(),
        description=description,
        body=body.strip(),
        path=path,
        args_hint=str(meta.get("argument-hint") or meta.get("argument_hint") or "").strip(),
        agent=str(meta.get("agent") or "").strip(),
        model=str(meta.get("model") or "").strip(),
        aliases=[a.lstrip("/").lower() for a in _as_list(meta.get("alias")) if a],
    )


def load_dirs(dirs: Optional[Sequence[Path]] = None) -> Tuple[List[CommandSpec], List[str]]:
    """Descubre comandos; devuelve ``(specs, errores)``. Nunca lanza."""
    specs: List[CommandSpec] = []
    errors: List[str] = []
    seen: set = set()
    for directory in dirs if dirs is not None else default_dirs():
        try:
            if not Path(directory).is_dir():
                continue
            entries = sorted(Path(directory).glob("*.md"))
        except OSError as exc:
            errors.append(f"{directory}: {exc}")
            continue
        for fp in entries:
            if fp.name.startswith(("_", ".")):
                continue
            if len(specs) >= MAX_COMMANDS:
                errors.append(f"límite de {MAX_COMMANDS} comandos alcanzado")
                return specs, errors
            try:
                st = fp.stat()
                if st.st_size > MAX_FILE_BYTES:
                    errors.append(f"{fp.name}: supera {MAX_FILE_BYTES} bytes")
                    continue
                key = str(fp)
                cached = _cache.get(key)
                if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                    spec = cached[2]
                else:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                    spec = parse_command(fp.stem, text, str(fp))
                    if spec is None:
                        errors.append(f"{fp.name}: nombre o frontmatter inválido")
                        continue
                    _cache[key] = (st.st_mtime, st.st_size, spec)
            except OSError as exc:
                errors.append(f"{fp.name}: {exc}")
                continue
            if spec.name in seen:
                errors.append(f"{fp.name}: nombre '{spec.name}' duplicado (se ignora)")
                continue
            seen.add(spec.name)
            specs.append(spec)
    return specs, errors


def _read_include(rel: str, root: Path) -> Tuple[Optional[str], Optional[str]]:
    """Lee un ``@ruta`` confinado a ``root``. Devuelve ``(texto, aviso)``."""
    try:
        candidate = (root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    except (OSError, ValueError) as exc:
        return None, f"@ {rel}: ruta inválida ({exc})"
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, f"@ {rel}: fuera del proyecto, no se incluye"
    try:
        if not candidate.is_file():
            return None, f"@ {rel}: no existe"
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"@ {rel}: {exc}"
    if len(text) > MAX_INCLUDE_CHARS:
        return text[:MAX_INCLUDE_CHARS], f"@ {rel}: recortado a {MAX_INCLUDE_CHARS} caracteres"
    return text, None


def render(spec: CommandSpec, args: Sequence[str], root: Optional[Path] = None) -> Tuple[str, List[str]]:
    """Expande el cuerpo del comando. Devuelve ``(prompt, avisos)``."""
    root = Path(root) if root is not None else project_root()
    warnings: List[str] = []
    body = spec.body
    joined = " ".join(args)
    body = body.replace("$ARGUMENTS", joined)

    def _pos(match: "re.Match[str]") -> str:
        idx = int(match.group(1))
        return args[idx - 1] if 1 <= idx <= len(args) else ""

    body = POSITIONAL_RE.sub(_pos, body)

    def _inc(match: "re.Match[str]") -> str:
        rel = match.group(1)
        content, warn = _read_include(rel, root)
        if warn:
            warnings.append(warn)
        if content is None:
            return match.group(0)
        return f"\n```{rel}\n{content}\n```\n"

    body = INCLUDE_RE.sub(_inc, body)
    return body.strip(), warnings


def refresh(registry, dirs: Optional[Sequence[Path]] = None) -> Dict[str, object]:
    """Registra los comandos del disco en ``registry`` (builtins no se pisan)."""
    from src.core.slash_commands import SlashCommand, SlashResult

    specs, errors = load_dirs(dirs)
    registered: List[str] = []
    skipped: List[str] = []
    for spec in specs:
        if registry.get(spec.name) is not None:
            skipped.append(f"{spec.name}: choca con un comando existente")
            continue
        clash = next((a for a in spec.aliases if registry.get(a) is not None), None)
        aliases = [a for a in spec.aliases if registry.get(a) is None]
        if clash:
            skipped.append(f"{spec.name}: alias '{clash}' ya existe (se ignora el alias)")

        def _make(locked: CommandSpec):
            async def _handler(args: List[str], ctx: Dict[str, object], _s: CommandSpec = locked) -> SlashResult:
                prompt, warnings = render(_s, args)
                if not prompt:
                    return SlashResult(ok=False, message=f"/{_s.name}: el comando no produce texto")
                data: Dict[str, object] = {
                    "action": "send_prompt",
                    "command": _s.name,
                    "prompt": prompt,
                }
                if _s.agent:
                    data["agent"] = _s.agent
                if _s.model:
                    data["model"] = _s.model
                if warnings:
                    data["warnings"] = warnings
                note = f"/{_s.name} listo ({len(prompt)} caracteres)"
                if warnings:
                    note += f" · {len(warnings)} aviso(s)"
                return SlashResult(ok=True, message=note, data=data)

            return _handler

        registry.register(
            SlashCommand(
                name=spec.name,
                aliases=aliases,
                description=spec.description,
                args=spec.args_hint,
                handler=_make(spec),
                origin="custom",
            )
        )
        registered.append(spec.name)
    return {"registered": registered, "skipped": skipped, "errors": errors}


def list_custom(dirs: Optional[Sequence[Path]] = None) -> List[Dict[str, object]]:
    specs, _ = load_dirs(dirs)
    return [s.to_dict() for s in specs]
