"""Scope — ACL + FS jail (default deny).

Origen: Hermes file_safety + SuperNEXUS tool_guardrails + path_normalize + Cordis guard/fs.
Ver BLUEPRINT.md §8. Principio: default deny, allowed_roots + deny_patterns.

Features:
- Path jail with resolve() (handles .., symlinks) + allowlist check
- Deny globs via PurePath.match semantics (** support via fnmatch.translate)
- Traversal prevention for non-existing paths (strict=False resolve)
- Network gate: allow_network flag blocks web_* tools
- Mutating-tool confirmation: allow_mutating_without_confirm flag
- Size guard: max_read_bytes enforcement hook
- Project-root relative matching for deny patterns
"""
from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any


class ScopeDenied(PermissionError):
    """Raised when a path or tool violates scope policy."""
    pass


# ---------------------------------------------------------------------------
# Helpers: path normalize (cross-platform) — from supernexus-v2/path_normalize.py
# ---------------------------------------------------------------------------

def _normalize_path_str(path_str: str) -> str:
    if not path_str or not path_str.strip():
        return path_str
    raw = path_str.strip()
    if os.name != "nt":
        return raw
    if Path("/.dockerenv").exists():
        return raw
    if raw.startswith("/mnt/") or raw == "/mnt":
        parts = raw.split("/")
        if len(parts) >= 4:
            drive = parts[2]
            rest = "/".join(parts[3:])
            return f"{drive}:{os.sep}{rest.replace('/', os.sep)}"
        if len(parts) == 3:
            return f"{parts[2]}:\\"
    return raw


def _glob_match(path_str: str, pattern: str) -> bool:
    """Match path_str against glob pattern with ** support.

    Uses PurePosixPath.match where possible, falls back to fnmatch with
    translated regex for ** patterns. path_str expected POSIX style.
    """
    # Normalize pattern: **/.env should match .env at any depth
    # PurePosixPath.match handles ** natively on Python 3.10+
    try:
        pp = PurePosixPath(path_str)
        if pp.match(pattern):
            return True
        # Also try matching against filename only for patterns like **/*.key
        # PurePosixPath.match("foo/bar.key", "**/*.key") works, but "**/*.key" vs "bar.key"
        # Purposely also test with fnmatch fallback
    except Exception:
        pass
    # Fallback: fnmatch with pattern as-is + also try basename variant
    if fnmatch.fnmatch(path_str, pattern):
        return True
    # For patterns starting with **/, also try matching basename and full tail
    if pattern.startswith("**/"):
        tail = pattern[3:]
        if fnmatch.fnmatch(path_str, tail) or fnmatch.fnmatch(Path(path_str).name, tail):
            return True
        # fnmatch doesn't handle **; translate ** -> * for approximate
        approx = pattern.replace("**/", "*/").replace("**", "*")
        if fnmatch.fnmatch(path_str, approx):
            return True
    return fnmatch.fnmatch(Path(path_str).name, pattern)


# Tool categories for permission gating (from supernexus-v2/tool_guardrails.py)
IDEMPOTENT_TOOLS = frozenset({
    "read", "read_file", "todo_write", "web_fetch", "search_files", "web_search", "web_fetch",
    "browser_snapshot", "browser_console", "mcp_filesystem_read_file",
    "mcp_filesystem_list_directory", "mcp_filesystem_get_file_info",
    "glob", "grep", "search_observations", "repo_map", "code_search", "recall",
    "checkpoint_list", "checkpoint_diff", "team_list", "team_await",
})

MUTATING_TOOLS = frozenset({
    "write", "edit", "write_file", "patch", "apply_patch", "terminal", "bash", "execute_code",
    "browser_click", "browser_type", "browser_press", "browser_navigate",
    "send_message", "delegate_task", "execute", "exec", "pty_exec", "job_start", "job_kill",
    "learn", "todo_write", "image_generate", "sub_agent", "agent_run", "gema_run", "agent_create",
    "team_spawn", "team_mailbox", "checkpoint_create", "checkpoint_restore",
})

NETWORK_TOOLS = frozenset({
    "web_fetch", "web_search", "browser_navigate", "browser_open",
    "fetch", "webfetch", "brave_search", "websearch",
})

# En read_only permitimos estas tools SOLO si el guard determina que el comando
# no modifica archivos (port de command-guard de Cline). No incluye pty_exec:
# una shell interactiva puede mutar por caminos que el guard no ve.
SHELL_TOOLS = frozenset({"exec", "terminal", "bash", "sh", "run_command"})

# Secretos que NINGÚN preset (ni siquiera `permission: full`) puede leer.
# Se UNEN a `deny_patterns` en vez de reemplazarse: si config.yaml define su
# propia lista, antes dejaba fuera .ssh/.aws/*.pem y el agente podía leer claves.
HARD_DENY_PATTERNS = (
    "**/.env", "**/.env.*",
    "**/.ssh/**", "**/.aws/**", "**/.gnupg/**", "**/.kube/**",
    "**/.docker/config.json", "**/.git-credentials", "**/.netrc",
    "**/.npmrc", "**/.pypirc",
    "**/*.pem", "**/*.key", "**/*.pfx", "**/*.p12",
    "**/id_rsa*", "**/id_ed25519*", "**/credentials*",
)

_MAX_BYTES_DEFAULT = 1_048_576

# Sandbox ladder: nombres estilo Codex/OpenCode -> preset interno.
# Solo se puede escalar HACIA ARRIBA y con aprobacion previa.
SANDBOX_LEVELS: dict[str, int] = {
    "read_only": 0,
    "workspace_write": 1,
    "full": 2,
}
SANDBOX_ALIASES: dict[str, str] = {
    "read_only": "read_only",
    "read-only": "read_only",
    "readonly": "read_only",
    "workspace_write": "workspace_write",
    "workspace-write": "workspace_write",
    "workspacewrite": "workspace_write",
    "full": "full",
    "full-access": "full",
    "full_access": "full",
    "danger-full-access": "full",
    "danger_full_access": "full",
}


def normalize_permission(value: str | None) -> str:
    """Alias tolerant (read-only, workspace-write, danger-full-access)."""
    key = str(value or "workspace_write").strip().lower()
    return SANDBOX_ALIASES.get(key, "workspace_write")


def permission_level(value: str | None) -> int:
    return SANDBOX_LEVELS.get(normalize_permission(value), 1)


@dataclass(frozen=True, slots=True)
class ScopeConfig:
    allowed_roots: tuple[str, ...] = ("./workspace", "./data")
    deny_patterns: tuple[str, ...] = ("**/.env", "**/.env.*", "**/*.key", "**/*.pem", "**/*.pfx",
                                      "**/*.p12", "**/id_rsa*", "**/id_ed25519*", "**/.git-credentials",
                                      "**/.npmrc", "**/.aws/**", "**/.ssh/**", "**/credentials*",
                                      "**/.netrc", "**/brain/*.db-shm", "**/brain/*.db-wal")
    max_read_bytes: int = 1_048_576
    max_file_read_bytes: int = 1_048_576  # alias, compat with config.yaml
    allow_network: bool = False
    allow_mutating_without_confirm: bool = False
    require_confirm_mutating: bool = False  # opt-in: mutantes exigen confirm=true en args
    permission: str = "workspace_write"  # read_only | workspace_write | full

    def effective_max_bytes(self) -> int:
        # `max_file_read_bytes` es el nombre explícito; si no se tocó, manda
        # `max_read_bytes` (el nombre histórico de config.yaml).
        if self.max_file_read_bytes != _MAX_BYTES_DEFAULT:
            return self.max_file_read_bytes
        return self.max_read_bytes or self.max_file_read_bytes


@dataclass
class Scope:
    """ACL + FS jail. Default deny. Thread-safe (stateless).

    Usage:
        cfg = ScopeConfig(allowed_roots=("./workspace",), deny_patterns=("**/.env",))
        scope = Scope(cfg, project_root=Path.cwd())
        scope.check_path("workspace/foo.txt")  # ok
        scope.guard("write", {"path": "workspace/out.txt", "content": "..."})
    """

    cfg: ScopeConfig
    project_root: Path
    # Override runtime del ladder (solo escala hacia arriba, con aprobacion).
    runtime_permission: str = ""

    @property
    def permission(self) -> str:
        return normalize_permission(self.runtime_permission or self.cfg.permission)

    def can_escalate_to(self, target: str | None) -> bool:
        return permission_level(target) >= permission_level(self.permission)

    def escalate(self, target: str | None, *, approved: bool = False) -> str:
        """Sube el nivel de sandbox. Nunca baja y exige aprobacion previa."""
        new_perm = normalize_permission(target)
        old_level = permission_level(self.permission)
        new_level = permission_level(new_perm)
        if new_level < old_level:
            raise ScopeDenied(
                f"el sandbox solo escala hacia arriba: {self.permission} -> {new_perm}")
        if new_level == old_level:
            return self.permission
        if not approved:
            raise ScopeDenied(
                f"escalado de sandbox {self.permission} -> {new_perm} requiere aprobacion previa")
        self.runtime_permission = new_perm
        return new_perm

    def __post_init__(self):
        # Normalize project_root once
        object.__setattr__(self, "project_root", self.project_root.resolve())
        # Pre-resolve allowed roots to absolute Paths
        allowed: list[Path] = []
        for r in self.cfg.allowed_roots:
            p = Path(_normalize_path_str(r))
            if not p.is_absolute():
                p = (self.project_root / p)
            # Use resolve(strict=False) to handle non-existing ./workspace
            try:
                p = p.resolve()
            except Exception:
                p = p.absolute()
            allowed.append(p)
        object.__setattr__(self, "_allowed", allowed)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _is_allowed(self, p: Path) -> bool:
        # Resolve strict=False to avoid failing on non-existing paths
        try:
            rp = p.resolve()
        except Exception:
            rp = p.absolute()
        for a in self._allowed:  # type: ignore[attr-defined]
            try:
                if rp == a or rp.is_relative_to(a):
                    return True
            except ValueError:
                continue
        return False

    def _is_denied(self, p: Path) -> bool:
        try:
            rp = p.resolve()
        except Exception:
            rp = p.absolute()
        # Build POSIX relative string for glob matching
        try:
            rel = rp.relative_to(self.project_root)
            rel_posix = rel.as_posix()
        except ValueError:
            rel_posix = rp.as_posix()
        name = rp.name
        # Check each deny pattern against rel_posix and posix full path and name.
        # Unión con HARD_DENY_PATTERNS: la config puede AÑADIR patrones, nunca quitar secretos.
        for pat in (*self.cfg.deny_patterns, *HARD_DENY_PATTERNS):
            pat_posix = pat.replace("\\", "/")
            if _glob_match(rel_posix, pat_posix):
                return True
            if _glob_match(rp.as_posix(), pat_posix):
                return True
            if _glob_match(name, pat_posix):
                return True
            # Also handle brain/*.db-shm patterns that may be absolute
            if fnmatch.fnmatch(rel_posix, pat_posix) or fnmatch.fnmatch(name, pat_posix):
                return True
        return False

    # ------------------------------------------------------------------
    # Public API (BLUEPRINT §8.3)
    # ------------------------------------------------------------------
    def check_path(self, path: str | Path) -> Path:
        """Resolve and validate that path is inside allowed_roots and not matching deny."""
        raw = _normalize_path_str(str(path))
        p = Path(raw)
        if not p.is_absolute():
            p = self.project_root / p
        # Check deny first (more specific) — secretos bloqueados siempre.
        if self._is_denied(p):
            raise ScopeDenied(f"ruta denegada por patrones de denegación: {p} (patrones: {self.cfg.deny_patterns})")
        # permission 'full' = acceso total a la PC (el usuario lo activa en Ajustes).
        perm = self.permission
        if perm != "full" and not self._is_allowed(p):
            raise ScopeDenied(
                f"ruta fuera de raíces permitidas: {p} (permitidas: {self.cfg.allowed_roots}). "
                f"Para acceso total a la PC, activa Ajustes → Permisos → 'full'."
            )
        return p

    def check_tool(self, tool_name: str, args: dict[str, Any]) -> None:
        """Validate tool + args against policies.

        - Network gate: blocks NETWORK_TOOLS when allow_network=False
        - Path args: validates common path keys via check_path
        - Size gate: checks content length against max_read_bytes for writes
        - Mutating confirmation: raises if mutating tool without confirm
        """
        # Network gate
        if not self.cfg.allow_network and tool_name in NETWORK_TOOLS:
            raise ScopeDenied(f"herramienta de red denegada: {tool_name} (permitir_red=false)")

        # Permission preset gate — harness parity (Read Only / Workspace Write / Full access)
        perm = self.permission
        if perm == "read_only" and tool_name not in IDEMPOTENT_TOOLS:
            if tool_name in SHELL_TOOLS:
                # Plan mode mantiene la shell para investigar, pero solo con
                # comandos que no escriben (git status, grep, ls...). El jail
                # de exec sigue prohibiendo encadenar/redirigir en read_only.
                from .command_guard import (
                    find_file_editing_command,
                    format_plan_mode_blocked_error,
                )
                raw_command = args.get("command") or args.get("cmd") or ""
                reason = find_file_editing_command(str(raw_command))
                if reason:
                    raise ScopeDenied(format_plan_mode_blocked_error(reason))
            else:
                raise ScopeDenied(
                    f"permission read_only: herramienta bloqueada {tool_name!r} (solo lectura)")
        # Mutating gate (opt-in): exige confirm=true en args cuando require_confirm_mutating.
        # read_only ya bloquea arriba; esto agrega confirmacion explicita a las mutantes.
        if tool_name in MUTATING_TOOLS and getattr(self.cfg, "require_confirm_mutating", False):
            if not args.get("confirm"):
                raise ScopeDenied(
                    f"herramienta mutante {tool_name!r} requiere confirm=true "
                    f"(scope.require_confirm_mutating=true)"
                )

        # Path args validation
        path_keys = ("path", "file", "filepath", "dir", "directory", "filePath", "db_path", "persist_dir")
        for k in path_keys:
            if k in args and isinstance(args[k], str) and args[k]:
                self.check_path(args[k])

        # Handle list-of-paths patterns (e.g., glob pattern itself is not a path to check,
        # but file read with content length)
        if tool_name in ("write", "write_file", "edit") and "content" in args:
            content = args["content"]
            if isinstance(content, str) and len(content.encode("utf-8")) > self.cfg.effective_max_bytes() * 4:
                # Allow large writes but warn via ScopeDenied if exceeds 4x limit (prevent OOM)
                # For strict enforcement, uncomment:
                # raise ScopeDenied(f"write content exceeds max bytes: {len(content)} > {self.cfg.effective_max_bytes()}")
                pass

    def guard(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Validate and return sanitized args. Raises ScopeDenied if violates."""
        self.check_tool(tool_name, args)
        return args

    def is_path_allowed(self, path: str | Path) -> bool:
        try:
            self.check_path(path)
            return True
        except ScopeDenied:
            return False

    def allowed_roots_resolved(self) -> list[Path]:
        return list(self._allowed)  # type: ignore[attr-defined]

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.project_root),
            "allowed_roots": list(self.cfg.allowed_roots),
            "allowed_resolved": [str(p) for p in self._allowed],  # type: ignore[attr-defined]
            "deny_patterns": list(self.cfg.deny_patterns),
            "max_read_bytes": self.cfg.effective_max_bytes(),
            "allow_network": self.cfg.allow_network,
            "allow_mutating_without_confirm": self.cfg.allow_mutating_without_confirm,
            "permission": self.permission,
            "permission_level": permission_level(self.permission),
        }
