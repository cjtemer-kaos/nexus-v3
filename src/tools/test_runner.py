"""Runner de tests con parseo de fallos (paridad con opencode/Claude Code).

`exec` devuelve la salida cruda: el modelo tiene que leer 400 lineas para
saber si fallaron 2 o 200 tests y cuales. Esto corre el runner del proyecto y
devuelve el resumen estructurado —conteos, primeros fallos con su mensaje— y
la salida recortada como respaldo.

Nunca usa shell: cada runner se arma como argv. El unico lugar con string
libre es `args`, y va como argumentos separados, no interpretados.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MAX_OUTPUT = 30_000
MAX_FAILURES = 25

_PYTEST_SUMMARY = re.compile(
    r"(?P<body>(?:\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed|deselected"
    r"|warnings?|warning)(?:,\s*and\s+|,\s*|\s+and\s+)?)+)\s+in\s+(?P<secs>[0-9.]+)s")
_PYTEST_NO_TESTS = re.compile(r"^no tests ran in (?P<secs>[0-9.]+)s", re.MULTILINE)
_PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warning|warnings)")
# Una linea de progreso de `-q`: puntos/letras, opcionalmente con `[ 50%]`.
_PYTEST_PROGRESS = re.compile(r"^(?P<marks>[.sFEx]+)(?:\s*\[\s*\d+%\])?\s*$", re.MULTILINE)
_PYTEST_FAILED_LINE = re.compile(r"^(?:FAILED|ERROR) (?P<name>\S+)(?: - (?P<msg>.*))?$", re.MULTILINE)
_JEST_SUMMARY = re.compile(r"^Tests:\s+(?P<body>.+)$", re.MULTILINE)
_JEST_COUNT = re.compile(r"(\d+) (passed|failed|skipped|todo|total)")
_JEST_FAIL_LINE = re.compile(r"^\s*(?:●|✕|×)\s+(?P<name>.+?)(?:\s*›\s*(?P<msg>.*))?$", re.MULTILINE)
_CARGO_SUMMARY = re.compile(r"^test result: (?P<status>\w+)\. (?P<body>.+)$", re.MULTILINE)
_CARGO_FAIL = re.compile(r"^---- (?P<name>\S+) stdout ----\n(?P<msg>.*?)(?=\n----|\Z)", re.MULTILINE | re.DOTALL)
_GO_FAIL = re.compile(r"^--- FAIL: (?P<name>\S+)(?: \(.*?\))?\n(?P<msg>(?:\s{4}.*\n?)*)", re.MULTILINE)


def _which(name: str) -> str | None:
    return shutil.which(name) or shutil.which(f"{name}.cmd") or shutil.which(f"{name}.exe")


def detect(root: Path) -> list[str]:
    """Runners que tienen sentido en este directorio, en orden de preferencia."""
    found: list[str] = []
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() \
            or (root / "setup.cfg").exists() or (root / "tests").is_dir() \
            or (root / "conftest.py").exists():
        if _which("pytest") or _module_available("pytest"):
            found.append("pytest")
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            import json as _json
            data = _json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            if (data.get("scripts") or {}).get("test"):
                found.append("npm")
        except Exception:
            found.append("npm")
    if (root / "Cargo.toml").exists():
        found.append("cargo")
    if (root / "go.mod").exists():
        found.append("go")
    return found


def _module_available(name: str) -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _argv_for(runner: str, root: Path, args: list[str] | None, keyword: str) -> list[str]:
    if runner == "pytest":
        argv = [sys.executable, "-m", "pytest", "-q"]
        if keyword:
            argv.extend(["-k", keyword])
        return argv + list(args or [])
    if runner == "npm":
        npm = _which("npm")
        if not npm:
            raise RuntimeError("npm no esta en PATH")
        return [npm, "test", "--silent", *list(args or [])]
    if runner == "cargo":
        cargo = _which("cargo")
        if not cargo:
            raise RuntimeError("cargo no esta en PATH")
        return [cargo, "test", *list(args or [])]
    if runner == "go":
        go = _which("go")
        if not go:
            raise RuntimeError("go no esta en PATH")
        return [go, "test", *(list(args) or ["./..."])]
    if runner == "custom":
        if not args:
            raise RuntimeError("runner=custom necesita args (lista de argumentos)")
        return list(args)
    raise RuntimeError(f"runner desconocido: {runner}")


def parse_pytest(output: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    summary_line = ""
    # El resumen aparece una sola vez, pero distintas versiones/modos lo
    # decoran distinto (`==== 1 failed, 1 passed in 0.2s ====` o pelado
    # `1 failed, 1 passed in 0.2s`). Se toma la ULTIMA coincidencia del texto.
    matches = list(_PYTEST_SUMMARY.finditer(output))
    if matches:
        m = matches[-1]
        summary_line = f"{m.group('body').strip()} in {m.group('secs')}s"
        for n, kind in _PYTEST_COUNT.findall(m.group("body")):
            key = "error" if kind in ("error", "errors") else kind.rstrip("s")
            counts[key] = counts.get(key, 0) + int(n)
    elif _PYTEST_NO_TESTS.search(output):
        summary_line = _PYTEST_NO_TESTS.search(output).group(0).strip()
    failures = []
    for mm in _PYTEST_FAILED_LINE.finditer(output):
        failures.append({"name": mm.group("name"), "detail": (mm.group("msg") or "").strip()[:600]})
    if not counts:
        # Sin banner (pytest viejo, salida cortada por timeout, o un `-q` que
        # no llego al resumen): se cuenta el progreso `.F s E x`.
        marks = "".join(grp.group("marks") for grp in _PYTEST_PROGRESS.finditer(output))
        if marks:
            counts = {"passed": marks.count("."), "failed": marks.count("F"),
                      "skipped": marks.count("s"), "error": marks.count("E")}
            counts = {k: v for k, v in counts.items() if v}
    return {
        "runner": "pytest",
        "summary_line": summary_line,
        "counts": counts,
        "failures": failures[:MAX_FAILURES],
        "failures_truncated": len(failures) > MAX_FAILURES,
    }


def parse_npm(output: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    summary_line = ""
    m = _JEST_SUMMARY.search(output)
    if m:
        summary_line = m.group("body").strip()
        for n, kind in _JEST_COUNT.findall(summary_line):
            counts[kind] = counts.get(kind, 0) + int(n)
    failures = []
    for mm in _JEST_FAIL_LINE.finditer(output):
        name = (mm.group("name") or "").strip()
        if not name:
            continue
        detail = (mm.group("msg") or "").strip()
        if name.lower() in ("console", "tests:", "test suites:"):
            continue
        failures.append({"name": name[:300], "detail": detail[:600]})
    return {
        "runner": "npm",
        "summary_line": summary_line,
        "counts": counts,
        "failures": failures[:MAX_FAILURES],
        "failures_truncated": len(failures) > MAX_FAILURES,
    }


def parse_cargo(output: str) -> dict[str, Any]:
    counts: dict[str, int] = {}
    lines = []
    for m in _CARGO_SUMMARY.finditer(output):
        lines.append(m.group(0).strip())
        for n, kind in re.findall(r"(\d+) (passed|failed|ignored)", m.group("body")):
            key = "skipped" if kind == "ignored" else kind
            counts[key] = counts.get(key, 0) + int(n)
    failures = []
    for m in _CARGO_FAIL.finditer(output):
        failures.append({"name": m.group("name"), "detail": m.group("msg").strip()[:600]})
    return {
        "runner": "cargo",
        "summary_line": " | ".join(lines),
        "counts": counts,
        "failures": failures[:MAX_FAILURES],
        "failures_truncated": len(failures) > MAX_FAILURES,
    }


def parse_go(output: str) -> dict[str, Any]:
    failures = []
    for m in _GO_FAIL.finditer(output):
        failures.append({"name": m.group("name"), "detail": m.group("msg").strip()[:600]})
    counts = {"failed": len(failures)} if failures else ({"passed": 1} if "ok " in output else {})
    return {
        "runner": "go",
        "summary_line": "",
        "counts": counts,
        "failures": failures[:MAX_FAILURES],
        "failures_truncated": len(failures) > MAX_FAILURES,
    }


_PARSERS = {"pytest": parse_pytest, "npm": parse_npm, "cargo": parse_cargo, "go": parse_go}


def run(root: Path, runner: str = "auto", args: list[str] | None = None,
        keyword: str = "", timeout: float = 600.0,
        max_output: int = MAX_OUTPUT) -> dict[str, Any]:
    """Corre los tests y devuelve conteos + fallos parseados + salida recortada."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"no es un directorio: {root}")
    detected = detect(root)
    if runner in ("auto", ""):
        if not detected:
            raise RuntimeError(
                f"no encontre un runner en {root}: ni pytest, ni scripts.test en "
                f"package.json, ni Cargo.toml/go.mod")
        runner = detected[0]
    argv = _argv_for(runner, root, args, keyword)
    t0 = time.time()
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        p = subprocess.run(
            argv, cwd=str(root), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=creation,
        )
        rc, out, err = p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired as e:
        parcial = ""
        if isinstance(e.stdout, bytes):
            parcial = e.stdout.decode("utf-8", "replace")
        elif isinstance(e.stdout, str):
            parcial = e.stdout
        return {
            "ok": False, "runner": runner, "command": argv,
            "exit_code": None, "timed_out": True, "timeout_s": timeout,
            "duration_s": round(time.time() - t0, 1),
            "error": f"el runner no terminó en {timeout:.0f}s (los tests quedaron corriendo?)",
            "output": parcial[:max_output],
        }
    duration = round(time.time() - t0, 1)
    merged = out + (("\n" + err) if err.strip() else "")
    parsed = _PARSERS.get(runner, lambda _o: {
        "runner": runner, "summary_line": "", "counts": {}, "failures": [],
        "failures_truncated": False,
    })(merged)
    failures = parsed.get("failures") or []
    # Exit 0 con fallos parseados no existe; exit != 0 sin fallos parseados
    # puede ser un error de coleccion (import roto) — se dice explicitamente.
    ok = rc == 0
    result: dict[str, Any] = {
        "ok": ok,
        "runner": runner,
        "command": argv,
        "exit_code": rc,
        "duration_s": duration,
        "detected": detected,
        **parsed,
        "output": merged[-max_output:] if len(merged) > max_output else merged,
        "output_truncated": len(merged) > max_output,
    }
    if not ok and not failures:
        result["hint"] = ("el runner falló sin un fallo de test identificable: suele ser un "
                          "error de colección/import. Mirá el final de `output`.")
    return result


def run_argv(argv: list[str], cwd: Path, timeout: float = 600.0) -> dict[str, Any]:
    """Escotilla para comandos de test armados a mano (ya tokenizados)."""
    return run(cwd, runner="custom", args=argv, timeout=timeout)


def split_command(command: str) -> list[str]:
    """Tokeniza un comando de test escrito por el usuario, sin pasar por shell."""
    return shlex.split(command, posix=(os.name != "nt"))
