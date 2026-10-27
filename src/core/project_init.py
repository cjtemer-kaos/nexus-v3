"""Generador de AGENTS.md — `/init` sobre un repo real.

El harness ya lee `AGENTS.md` de forma jerárquica (`agents_md.py`), pero no
había con qué crear el primero. Esto mira lo que hay en disco y escribe un
borrador útil: stack, comandos de test/build, estructura y reglas de higiene.

Principio: **no inventar**. Cada línea sale de un archivo que existe
(`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`…) o de un directorio
que se ve. Lo que el código no puede saber queda como sección a completar, en
vez de rellenarse con reglas genéricas que después el agente se cree.

No lee secretos: solo existencia de archivos y los `scripts` de
`package.json`. Nunca abre `.env`, `.pem` ni nada por el estilo.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", "out", ".next", ".nuxt", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "release", "vendor", "bin", "obj",
}
MAX_DIRS = 12
MAX_ENTRY_POINTS = 6
AGENTS_FILE = "AGENTS.md"
MAX_BYTES = 200_000


@dataclass
class ProjectInfo:
    root: Path
    stacks: List[str] = field(default_factory=list)
    test_command: str = ""
    build_command: str = ""
    run_command: str = ""
    package_manager: str = ""
    dirs: List[str] = field(default_factory=list)
    entry_points: List[str] = field(default_factory=list)
    manifest: str = ""
    has_existing: bool = False


def _read_text(path: Path, limit: int = MAX_BYTES) -> str:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _package_manager(root: Path) -> str:
    for lock, name in (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lockb", "bun"), ("package-lock.json", "npm")):
        if (root / lock).is_file():
            return name
    return "npm" if (root / "package.json").is_file() else ""


def _node_scripts(root: Path) -> Dict[str, str]:
    raw = _read_text(root / "package.json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    scripts = data.get("scripts")
    return {str(k): str(v) for k, v in scripts.items()} if isinstance(scripts, dict) else {}


def _top_dirs(root: Path) -> List[str]:
    out: List[str] = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name.startswith("_"):
                continue
            if child.name.lower() in IGNORED_DIRS:
                continue
            out.append(child.name + "/")
            if len(out) >= MAX_DIRS:
                break
    except OSError:
        pass
    return out


def _entry_points(root: Path) -> List[str]:
    candidates = [
        "main.py", "app.py", "src/main.py", "src/app.py", "manage.py",
        "src/index.ts", "src/index.js", "index.js", "index.ts",
        "src/main.rs", "main.go", "cmd", "Dockerfile", "docker-compose.yml",
    ]
    out = [c for c in candidates if (root / c).exists()]
    return out[:MAX_ENTRY_POINTS]


def analyze(root: str | Path) -> ProjectInfo:
    """Lee el repo y devuelve lo que se pudo comprobar. No escribe nada."""
    root = Path(root).resolve()
    info = ProjectInfo(root=root)
    pyproject = root / "pyproject.toml"
    has_py = pyproject.is_file() or (root / "setup.py").is_file() or (root / "requirements.txt").is_file()
    has_node = (root / "package.json").is_file()
    has_rust = (root / "Cargo.toml").is_file()
    has_go = (root / "go.mod").is_file()

    if has_py:
        info.stacks.append("Python")
        info.manifest = "pyproject.toml" if pyproject.is_file() else ("setup.py" if (root / "setup.py").is_file() else "requirements.txt")
        if (root / "tests").is_dir() or (root / "test").is_dir():
            info.test_command = "python -m pytest -q"
        if (root / "ruff.toml").is_file() or ".ruff" in _read_text(pyproject):
            info.build_command = "python -m ruff check ."
    if has_node:
        info.stacks.append("Node.js")
        info.manifest = info.manifest or "package.json"
        info.package_manager = _package_manager(root)
        scripts = _node_scripts(root)
        pm = info.package_manager or "npm"
        runner = f"{pm} run" if pm in {"npm", "pnpm", "yarn", "bun"} else "npm run"
        if "test" in scripts:
            info.test_command = info.test_command or f"{runner} test"
        if "build" in scripts:
            info.build_command = info.build_command or f"{runner} build"
        if "dev" in scripts:
            info.run_command = f"{runner} dev"
        elif "start" in scripts:
            info.run_command = f"{runner} start"
    if has_rust:
        info.stacks.append("Rust")
        info.manifest = info.manifest or "Cargo.toml"
        info.test_command = info.test_command or "cargo test"
        info.build_command = info.build_command or "cargo build"
        info.run_command = info.run_command or "cargo run"
    if has_go:
        info.stacks.append("Go")
        info.manifest = info.manifest or "go.mod"
        info.test_command = info.test_command or "go test ./..."
        info.build_command = info.build_command or "go build ./..."

    info.dirs = _top_dirs(root)
    info.entry_points = _entry_points(root)
    info.has_existing = (root / AGENTS_FILE).is_file()
    return info


def render(info: ProjectInfo) -> str:
    """Borrador de AGENTS.md en español, solo con datos comprobados."""
    nombre = info.root.name or "proyecto"
    stack = ", ".join(info.stacks) if info.stacks else "sin detectar"
    lineas: List[str] = [
        f"# AGENTS.md — {nombre}",
        "",
        f"Stack detectado: **{stack}**"
        + (f" · manifiesto: `{info.manifest}`" if info.manifest else ""),
        "",
        "> Este archivo lo generó `/init` mirando lo que hay en disco. Completá lo que",
        "> el código no puede saber (decisiones, convenciones, qué NO tocar).",
        "",
        "## Estructura",
        "",
    ]
    if info.dirs:
        lineas.extend(f"- `{d}`" for d in info.dirs)
    else:
        lineas.append("- (sin subdirectorios relevantes)")
    if info.entry_points:
        lineas += ["", "Puntos de entrada:", ""]
        lineas.extend(f"- `{p}`" for p in info.entry_points)

    lineas += ["", "## Comandos", ""]
    if info.test_command:
        lineas += ["```bash", info.test_command, "# toda la suite; tenés que terminarla en exit 0 antes de dar algo por hecho", "```", ""]
    if info.build_command:
        lineas += ["```bash", info.build_command, "```", ""]
    if info.run_command:
        lineas += ["```bash", info.run_command, "```", ""]
    if not (info.test_command or info.build_command or info.run_command):
        lineas += ["_(no se detectó un runner: agregá acá cómo se corre y cómo se prueba)_", ""]

    lineas += [
        "## Reglas",
        "",
        "1. **Verificar, no suponer**: corré los comandos de arriba y mirá la salida.",
        "2. **Nada hardcodeado**: configuración por variable de entorno o archivo.",
        "3. **Sin secretos en el repo**: ni claves, ni `.env`, ni tokens en el código.",
        "4. **Un cambio, un commit**: mensaje que explique el por qué, no el qué.",
        "5. Completá esta sección con lo que este proyecto tiene de particular.",
        "",
    ]
    return "\n".join(lineas)


def write_agents_md(root: str | Path, force: bool = False) -> Dict[str, object]:
    """Genera AGENTS.md. Con uno existente y sin ``force`` no lo pisa."""
    info = analyze(root)
    destino = info.root / AGENTS_FILE
    if info.has_existing and not force:
        return {
            "created": False,
            "path": str(destino),
            "reason": "ya existe AGENTS.md (pasá force=true para reemplazarlo)",
            "stacks": info.stacks,
        }
    texto = render(info)
    try:
        destino.write_text(texto, encoding="utf-8")
    except OSError as exc:
        return {"created": False, "path": str(destino), "reason": f"{type(exc).__name__}: {exc}", "stacks": info.stacks}
    return {
        "created": True,
        "path": str(destino),
        "bytes": len(texto.encode("utf-8")),
        "stacks": info.stacks,
        "replaced": bool(info.has_existing),
    }
