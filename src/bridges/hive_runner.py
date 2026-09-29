"""
Hive Runner — subprocess wrapper that invokes one agent CLI per task.

Design: 100% CLI. Each agent's command is a subprocess invocation; we capture
stdout/stderr, time it, return a JSON envelope. The runner itself never opens
HTTP/SSH/WS connections — the agent's command line does that for us.

This is the single source of truth for "how do I call agent X with prompt Y".
All dispatch paths (REST, WS, MCP, UI) go through run_agent() here.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

HIVE_CONFIG = Path(__file__).resolve().parent.parent.parent / "hive_agents.json"


@dataclass
class HiveResult:
    """JSON envelope returned by run_agent() — stable contract for all dispatch paths."""
    ok: bool
    agent: str
    task: str
    reply: str = ""
    raw_stdout: str = ""
    raw_stderr: str = ""
    exit_code: int = -1
    duration_ms: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Legacy project paths that must NEVER appear in cwd/binary/args of the registry.
# SuperNEXUS v2 is the canonical project. The old `nexus-ia` (and other pre-v2 dirs)
# are read-only references in /legacy, /deploy, AGENTS.md notes — they MUST NOT be
# used as runtime paths.
_LEGACY_PROJECT_PATHS = (
    # Legacy paths - replaced with env var
)


def _validate_registry(reg: dict[str, Any]) -> list[str]:
    """Return a list of warnings about legacy/wrong paths in the registry.
    Called once at load time. Non-fatal — the registry still loads, but the
    issues surface in /api/hive/status so they can be fixed."""
    warnings: list[str] = []
    for agent_name, cfg in reg.get("agents", {}).items():
        if not isinstance(cfg, dict):
            continue
        # Check every string-typed path-ish field
        for key in ("cwd", "binary"):
            v = cfg.get(key)
            if isinstance(v, str):
                for legacy in _LEGACY_PROJECT_PATHS:
                    if legacy.lower() in v.lower():
                        warnings.append(
                            f"agent '{agent_name}': {field}='{v}' points to a LEGACY project path ({legacy}). "
                            f"Use NEXUS_PROJECT_DIR instead."
                        )
        # Also scan args list (paths often hide there)
        for i, a in enumerate(cfg.get("args", []) or []):
            if isinstance(a, str):
                for legacy in _LEGACY_PROJECT_PATHS:
                    if legacy.lower() in a.lower():
                        warnings.append(
                            f"agent '{agent_name}': args[{i}]='{a}' contains legacy project path ({legacy}). "
                            f"Update to NEXUS_PROJECT_DIR."
                        )
    return warnings


_REGISTRY_WARNINGS: list[str] = []


def load_registry() -> dict[str, Any]:
    """Load hive_agents.json — the declarative agent registry.
    On first call, validates all cwd/binary/args paths and caches any legacy-path warnings."""
    global _REGISTRY_WARNINGS
    if not HIVE_CONFIG.exists():
        log.warning(f"hive_agents.json not found at {HIVE_CONFIG}")
        return {"agents": {}, "version": "2.0"}
    reg = json.loads(HIVE_CONFIG.read_text(encoding="utf-8"))
    if not _REGISTRY_WARNINGS:
        _REGISTRY_WARNINGS = _validate_registry(reg)
        for w in _REGISTRY_WARNINGS:
            print(f"[hive_runner] WARNING: {w}", flush=True)
    return reg


def get_registry_warnings() -> list[str]:
    """Return any legacy-path warnings from the last registry load."""
    return list(_REGISTRY_WARNINGS)


def get_agent(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
    cfg = registry.get("agents", {}).get(name)
    if not cfg:
        return None
    if not cfg.get("enabled", True):
        return None
    return cfg


def _expand_args(args: list[str], task: str) -> list[str]:
    """Substitute {task} placeholder in arg templates; respect shell escaping rules."""
    out = []
    for a in args:
        if "{task}" in a:
            out.append(a.replace("{task}", task))
        else:
            out.append(a)
    return out


def _apply_post_parse(reply: str, parser: str | None) -> str:
    """Apply a post-parser to clean the agent's raw stdout into a user-facing reply.

    Known parsers:
      - 'antigravity_bridge': expects a JSON envelope with {status, result, message}
      - 'jsonpath:$.<key>.<sub>': extracts a nested field from a JSON envelope
      - None / unknown: returns reply stripped
    """
    text = (reply or "").strip()
    if not parser:
        return text
    if parser == "antigravity_bridge":
        try:
            data = json.loads(text)
        except Exception:
            return text
        status = data.get("status")
        if status == "success":
            result = data.get("result", {})
            if isinstance(result, dict):
                return str(result.get("reply") or result.get("text") or result.get("content") or json.dumps(result, ensure_ascii=False))
            return str(result)
        return f"[{status}] {data.get('message', '')}"
    if parser.startswith("jsonpath:$."):
        try:
            data = json.loads(text)
        except Exception:
            return text
        key = parser[len("jsonpath:$."):]
        for part in key.split("."):
            if isinstance(data, list):
                try:
                    data = data[int(part)]
                except (ValueError, IndexError):
                    return text
            elif isinstance(data, dict):
                data = data.get(part, "")
            else:
                return text
        return str(data) if data != "" else text
    return text


def run_agent(agent_name: str, task: str, *, timeout_s: int | None = None) -> HiveResult:
    """
    Run a single agent CLI with `task` substituted into its arg template.

    Returns a HiveResult (JSON-serializable). Never raises on agent failure —
    failures are encoded as ok=False with error/exit_code set.
    """
    registry = load_registry()
    cfg = get_agent(registry, agent_name)
    if not cfg:
        return HiveResult(
            ok=False,
            agent=agent_name,
            task=task,
            error=f"agent '{agent_name}' not found or disabled in hive_agents.json",
        )

    args = _expand_args(cfg["args"], task)
    cwd = cfg.get("cwd") or None
    # Always inherit the full parent env. Some CLIs (notably OpenSSH on
    # Windows) need HOME/HOMEDRIVE/USERPROFILE + agent-specific vars
    # (SSH_AUTH_SOCK, etc.) to be present; stripping to a whitelist
    # breaks them silently (exit 255, no stderr).
    # Agent-specific env entries are merged on top.
    env = dict(os.environ)
    agent_env = cfg.get("env") or {}
    env.update(agent_env)
    final_timeout = timeout_s or cfg.get("timeout_s") or registry.get("defaults", {}).get("timeout_s", 120)
    encoding = registry.get("defaults", {}).get("encoding", "utf-8")

    # Resolve binary path on Windows (handles .bat/.cmd where CreateProcess
    # does not consult PATHEXT for non-shell invocations).
    binary_path = shutil.which(cfg["binary"])
    if binary_path is None and os.name == "nt":
        for ext in (".bat", ".cmd", ".exe", ".BAT", ".CMD", ".EXE"):
            candidate = cfg["binary"] + ext
            if shutil.which(candidate):
                binary_path = shutil.which(candidate)
                break

    result = HiveResult(ok=False, agent=agent_name, task=task, started_at=time.time())

    # stdin payload: prefer explicit `stdin` template, else `stdin_json` to wrap
    # the raw task in {"task": ...}, else None.
    stdin_payload = cfg.get("stdin")
    if stdin_payload is None and cfg.get("stdin_json"):
        stdin_payload = json.dumps({"task": task})
    # subprocess.run with text=True expects str input. We pass str and let
    # Python handle encoding — passing bytes here raises "TypeError: write()
    # argument must be str, not bytes" because text mode forces str on stdin.

    try:
        proc = subprocess.run(
            [binary_path or cfg["binary"]] + args,
            cwd=cwd,
            env=env,
            input=stdin_payload,
            capture_output=True,
            text=True,
            timeout=final_timeout,
            encoding=encoding,
            errors="replace",
        )
        result.raw_stdout = proc.stdout or ""
        result.raw_stderr = proc.stderr or ""
        result.exit_code = proc.returncode
        result.ok = proc.returncode == 0
        if not result.ok:
            result.error = f"exit_code={proc.returncode}"
        result.reply = _apply_post_parse(result.raw_stdout, cfg.get("post_parse"))
    except subprocess.TimeoutExpired as e:
        result.error = f"timeout after {final_timeout}s"
        result.raw_stdout = (e.stdout.decode(encoding, errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""))
        result.raw_stderr = (e.stderr.decode(encoding, errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""))
        result.exit_code = -1
    except FileNotFoundError:
        result.error = f"binary '{cfg['binary']}' not found in PATH (resolved: {binary_path})"
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
    finally:
        result.finished_at = time.time()
        result.duration_ms = int((result.finished_at - result.started_at) * 1000)

    return result


def list_agents() -> list[dict[str, Any]]:
    """Return a summary list of all configured agents (enabled + disabled)."""
    registry = load_registry()
    out = []
    for name, cfg in registry.get("agents", {}).items():
        out.append({
            "name": name,
            "type": cfg.get("type"),
            "enabled": cfg.get("enabled", True),
            "description": cfg.get("description", ""),
            "tags": cfg.get("tags", []),
            "timeout_s": cfg.get("timeout_s"),
            "note": cfg.get("_note", ""),
        })
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "--list":
        for a in list_agents():
            mark = "x" if a["enabled"] else "."
            print(f" [{mark}] {a['name']:<14} {a['description'][:60]}")
        sys.exit(0)
    if len(sys.argv) < 3:
        print("usage: python -m src.bridges.hive_runner <agent> <task>")
        print("       python -m src.bridges.hive_runner --list")
        sys.exit(1)
    agent, task = sys.argv[1], sys.argv[2]
    res = run_agent(agent, task)
    print(json.dumps(res.to_dict(), ensure_ascii=False, indent=2))
    sys.exit(0 if res.ok else 2)
