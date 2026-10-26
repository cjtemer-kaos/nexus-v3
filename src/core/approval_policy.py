"""Clasificacion de acciones y gate humano para el despacho de tools.

Este modulo NO ejecuta nada: decide si una tool requiere aprobacion, aplica
grants (deny-first) y, si hay canal interactivo, encola el pedido y espera.

Orden de evaluacion (fail-closed):
  1. hard deny (secretos/comandos catastroficos): ni un grant lo vence.
  2. deny grant: gana sobre cualquier allow.
  3. allow grant (once/session/always).
  4. politica: auto | assisted | strict | never.
  5. pedido humano; sin UI -> deny inmediato (no se bloquea el proceso).
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("nexus-approval")

# Acciones canonicas que siempre piden aprobacion (greenlight).
_DANGER: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("force_push", re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)(?:\s|$)", re.I)),
    ("reset_hard", re.compile(r"\bgit\s+reset\s+--hard\b", re.I)),
    ("clean_force", re.compile(r"\bgit\s+clean\b[^\n]*-[a-z]*f", re.I)),
    ("publish", re.compile(r"\b(?:npm|pnpm|yarn)\s+publish\b", re.I)),
    ("publish", re.compile(r"\btwine\s+upload\b", re.I)),
    ("publish", re.compile(r"\bgh\s+release\s+create\b", re.I)),
    ("docker_push", re.compile(r"\bdocker\s+push\b", re.I)),
    ("docker_prune", re.compile(r"\bdocker\s+(?:system\s+)?prune\b", re.I)),
    ("kubectl_delete", re.compile(r"\bkubectl\s+delete\b", re.I)),
    ("terraform_apply", re.compile(r"\bterraform\s+(?:apply|destroy)\b", re.I)),
    ("cloud_delete", re.compile(r"\baws\b[^\n]*\bdelete-", re.I)),
    ("cloud_delete", re.compile(r"\bgcloud\b[^\n]*\bdelete\b", re.I)),
    ("cloud_delete", re.compile(r"\baz\b[^\n]*\bdelete\b", re.I)),
    ("pipe_shell", re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:ba|z|k)?sh\b", re.I)),
    ("kill_force", re.compile(r"\btaskkill\b[^\n]*\/f\b", re.I)),
    ("kill_force", re.compile(r"\bkill\s+-9\b", re.I)),
    # destructivos: normalmente el exec ya los bloquea; aqui quedan como
    # hard-deny para cualquier otro camino (MCP/shell remoto).
    ("destructive_file", re.compile(r"\brm\s+-rf\b", re.I)),
    ("destructive_file", re.compile(r"\bdel\s+\/[fs]\s+\/[sq]\b", re.I)),
    ("destructive_file", re.compile(r"\bformat\s+[a-z]:", re.I)),
    ("destructive_file", re.compile(r"\bmkfs\b", re.I)),
    ("destructive_file", re.compile(r"\bshutdown\b|\breboot\b|\bdiskpart\b", re.I)),
    ("destructive_file", re.compile(r"\breg\s+delete\b", re.I)),
    ("destructive_file", re.compile(r"\brd\s+\/s\b|\brmdir\s+\/s\b", re.I)),
    ("destructive_file", re.compile(r"remove-item\b[^\n]*-recurse", re.I)),
)

# Estos 4 patrones no se pueden aprobar: son catastroficos o irreversibles.
_HARD_DENY: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf\s+(?:\/|~|\$home|\$env:userprofile)(?:\s|$)", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r":\(\)\s*\{", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\b(?:shutdown|reboot|diskpart|bcdedit)\b", re.I),
    re.compile(r"\bdel\s+\/[fs]\s+\/[sq]\s+[a-z]:\\", re.I),
    re.compile(r"\brd\s+\/s\s+\/q\s+[a-z]:\\", re.I),
)

# Tools que ya tienen su propio gate en el handler (auto_apply=approve).
_HANDLER_GATED = frozenset({"write_file", "edit", "write"})
_READONLY_MCP_TOOLS = frozenset({
    "mcp_list", "mcp_ping", "mcp_describe", "mcp_tools",
})

# Defaults de solo lectura para MCP (se pueden ampliar en config).
_DEFAULT_MCP_READ_ONLY = (
    "brave-search/*",
    "parallel-search/*",
    "context7/*",
    "mcp-local-rag/*",
    "rea/*",
    "github/get_*",
    "github/list_*",
    "github/search_*",
    "hindsight/recall",
    "hindsight/search",
    "nexus-sovereign/read_*",
    "nexus-sovereign/list_*",
    "nexus-sovereign/search_*",
    "nexus-sovereign/brain_recall",
    "serena/find_*",
    "serena/search_*",
    "playwright/browser_snapshot",
    "playwright/browser_take_screenshot",
    "playwright/browser_console_messages",
    "playwright/browser_network_requests",
    "chrome-devtools/list_*",
    "chrome-devtools/get_*",
    "chrome-devtools/take_snapshot",
    "chrome-devtools/take_screenshot",
    "chrome-devtools/performance_*",
    "computer-control/get_*",
    "computer-control/list_*",
    "computer-control/take_screenshot",
    "computer-control/screenshot",
    "computer-control/read_*",
)

_MANIFEST_RO_CACHE: dict[str, Any] = {"mtime": None, "patterns": ()}


def _manifest_read_only() -> tuple[str, ...]:
    """Patrones `read_only` declarados por cada entrada del bundle (manifest).

    El manifest es la fuente de verdad de que tools de cada MCP son de lectura;
    asi el gate no bloquea de entrada servers legitimos como computer-control.
    """
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    p = Path(os.environ.get("NEXUS_MCP_MANIFEST")
             or (root / "data" / "mcp_bundle" / "manifest.json"))
    try:
        st = p.stat()
    except OSError:
        return ()
    if _MANIFEST_RO_CACHE["mtime"] == st.st_mtime_ns:
        return _MANIFEST_RO_CACHE["patterns"]
    out: list[str] = []
    try:
        import json

        data = json.loads(p.read_text(encoding="utf-8"))
        for entry in data.get("entries", []):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            for pat in entry.get("read_only") or []:
                pat = str(pat).strip()
                if pat:
                    out.append(f"{name}/{pat}")
    except Exception:
        out = []
    _MANIFEST_RO_CACHE.update(mtime=st.st_mtime_ns, patterns=tuple(out))
    return _MANIFEST_RO_CACHE["patterns"]


@dataclass(frozen=True)
class ApprovalCheck:
    required: bool = False
    hard_deny: bool = False
    action: str = ""
    reason: str = ""
    key_hash: str = ""
    preview: str = ""


@dataclass
class ApprovalContext:
    session_id: str = ""
    source: str = "unknown"
    interactive: bool = False
    policy: str = "assisted"          # auto | assisted | strict | never
    timeout_s: float = 120.0
    auto_apply: str = "assisted"
    config: dict = field(default_factory=dict)
    event_store: Any = None
    # Sesion padre (subagentes): sus grants `deny` se heredan; sus `allow` NO
    # (el hijo no puede escalar permisos por un allow del padre).
    parent_session_id: str = ""

    @property
    def mcp_read_only(self) -> tuple[str, ...]:
        acfg = (self.config.get("approval") or {}) if isinstance(self.config, dict) else {}
        extra = acfg.get("mcp_read_only")
        merged: list[str] = []
        if isinstance(extra, list):
            merged.extend(str(x) for x in extra)
        merged.extend(_DEFAULT_MCP_READ_ONLY)
        merged.extend(_manifest_read_only())
        seen: set[str] = set()
        out: list[str] = []
        for p in merged:
            if p not in seen:
                seen.add(p)
                out.append(p)
        return tuple(out)


@dataclass
class ApprovalOutcome:
    allowed: bool
    decision: str                    # allow | denied | timed_out | error
    reason: str = ""
    request_id: str = ""
    action: str = ""


def _cfg(ctx: ApprovalContext | None) -> dict:
    return (ctx.config if ctx and isinstance(ctx.config, dict) else {})


def _acfg(ctx: ApprovalContext | None) -> dict:
    cfg = _cfg(ctx)
    return cfg.get("approval") or {}


def _mcp_match(pattern: str, server: str, tool: str) -> bool:
    value = f"{server}/{tool}"
    return fnmatch.fnmatchcase(value, pattern) or fnmatch.fnmatchcase(server, pattern)


def _command_text(args: dict) -> str:
    for key in ("command", "cmd", "shell", "script"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
    return ""


def classify_command(command: str) -> tuple[str, str, bool]:
    """Devuelve (action, reason, hard_deny). 'exec' si no hay patron."""
    text = command or ""
    for pat in _HARD_DENY:
        if pat.search(text):
            return "destructive_file", f"comando catastrofico bloqueado: {pat.pattern}", True
    for action, pat in _DANGER:
        if pat.search(text):
            return action, f"accion sensible: {action}", False
    return "exec", "", False


def _make_key(tool: str, action: str, args: dict) -> str:
    from .approvals import fingerprint
    return fingerprint(tool, action, args)


def check_tool_approval(
    tool: str,
    args: dict | None,
    entry: Any = None,
    ctx: ApprovalContext | None = None,
) -> ApprovalCheck:
    """Clasifica una tool ANTES de ejecutarla. No consulta grants."""
    args = args or {}
    policy = (ctx.policy if ctx else "assisted") or "assisted"
    auto_apply = (ctx.auto_apply if ctx else "assisted") or "assisted"
    category = getattr(entry, "category", "") or ""

    # MCP: `mcp_call` lleva server/tool en args; el resto de tools MCP son
    # read-only por nombre. La allowlist es por server/tool.
    if tool == "mcp_call":
        server = str(args.get("server") or "").strip()
        remote_tool = str(args.get("tool") or "").strip()
        action = f"mcp:{server}:{remote_tool}" if server or remote_tool else "mcp:unknown"
        if server and remote_tool:
            ro = (ctx.mcp_read_only if ctx else _DEFAULT_MCP_READ_ONLY)
            if any(_mcp_match(p, server, remote_tool) for p in ro):
                return ApprovalCheck(False, action=action)
        if policy in ("auto",):
            return ApprovalCheck(False, action=action)
        preview = json.dumps({"server": server, "tool": remote_tool,
                              "arguments": args.get("arguments") or {}},
                             ensure_ascii=False, default=str)[:2000]
        return ApprovalCheck(True, action=action,
                             reason=(f"MCP sin allowlist de solo lectura: {server}/{remote_tool}"
                                     f" — agregá '{server}/{remote_tool}' a approval.mcp_read_only"
                                     " en config/config.yaml, o aprobalo desde la UI"),
                             key_hash=_make_key(tool, action, args), preview=preview)
    if category == "mcp" and tool not in _READONLY_MCP_TOOLS:
        action = f"mcp-tool:{tool}"
        if policy == "auto":
            return ApprovalCheck(False, action=action)
        return ApprovalCheck(True, action=action, reason="tool MCP sin allowlist",
                             key_hash=_make_key(tool, action, args),
                             preview=json.dumps(args, ensure_ascii=False, default=str)[:2000])
    if tool in _READONLY_MCP_TOOLS:
        return ApprovalCheck(False, action=tool)

    command = _command_text(args)
    action = tool
    reason = ""
    hard = False
    if tool in ("exec", "terminal", "bash", "shell") and command:
        action, reason, hard = classify_command(command)
        if hard:
            return ApprovalCheck(True, hard_deny=True, action=action,
                                 reason=reason, preview=command[:2000])
        if not reason and policy != "strict":
            return ApprovalCheck(False, action="exec")

    # Escrituras: write_file/edit ya tienen gate en el handler cuando
    # auto_apply=approve. apply_patch NO lo tiene -> se gatea en strict.
    if tool in _HANDLER_GATED and auto_apply == "approve":
        return ApprovalCheck(False, action=action)
    if tool == "apply_patch" and auto_apply == "approve":
        return ApprovalCheck(True, action="apply_patch",
                             reason="parche multi-archivo (auto_apply=approve)",
                             key_hash=_make_key(tool, action, args),
                             preview=str(args.get("patch") or "")[:2000])

    if not reason and policy == "strict":
        from .scope import MUTATING_TOOLS
        if tool in MUTATING_TOOLS:
            reason = f"tool mutante en modo strict: {tool}"
        elif category in ("local", "registered") and tool not in (
                "echo", "read_file", "list_dir", "glob", "code_search",
                "web_search", "web_fetch", "recall", "todo_write"):
            # strict = cualquier tool que no sea de lectura conocida
            reason = f"tool no clasificada como lectura: {tool}"

    if not reason:
        return ApprovalCheck(False, action=action)

    preview = command[:2000] if command else json.dumps(
        args, ensure_ascii=False, default=str)[:2000]
    return ApprovalCheck(True, action=action, reason=reason,
                         key_hash=_make_key(tool, action, args), preview=preview)


def check_hard_deny_config(tool: str, action: str, ctx: ApprovalContext | None) -> bool:
    """`approval.hard_deny` = patrones tool:action que ningun grant puede vencer."""
    acfg = _acfg(ctx)
    patterns = acfg.get("hard_deny") or []
    if not isinstance(patterns, list):
        return False
    value = f"{tool}:{action}"
    return any(fnmatch.fnmatchcase(value, str(p)) for p in patterns)


def _event(ctx: ApprovalContext | None, kind: str, data: dict) -> None:
    if ctx is None or ctx.event_store is None:
        return
    try:
        ctx.event_store.append_event(kind, data, session_id=ctx.session_id)
    except Exception:
        pass


async def authorize(
    check: ApprovalCheck,
    ctx: ApprovalContext | None,
    *,
    tool: str = "",
) -> ApprovalOutcome:
    """Aplica grants + politica. Puede esperar al humano si hay canal."""
    if not check.required:
        return ApprovalOutcome(True, "allow", action=check.action)
    if check.hard_deny or (ctx is not None and check_hard_deny_config(tool, check.action, ctx)):
        _event(ctx, "approval_denied", {"tool": tool, "action": check.action,
                                        "reason": check.reason, "hard": True})
        return ApprovalOutcome(False, "denied", check.reason or "hard deny",
                               action=check.action)

    from .approvals import get_approvals
    approvals = get_approvals()
    session_id = ctx.session_id if ctx else ""
    parent_sid = getattr(ctx, "parent_session_id", "") if ctx else ""
    if parent_sid and parent_sid != session_id:
        # Herencia de deny del padre: si el padre nego esta accion, el hijo
        # tampoco puede. Los allow del padre no se heredan (no escalar).
        pv = approvals.check_grant(
            session_id=parent_sid, tool=tool, action=check.action,
            key_hash=check.key_hash,
        )
        if pv == "deny":
            _event(ctx, "approval_denied", {"tool": tool, "action": check.action,
                                            "reason": "grant deny del padre"})
            return ApprovalOutcome(False, "denied",
                                   "denegado por un grant previo del padre",
                                   action=check.action)

    verdict = approvals.check_grant(
        session_id=session_id, tool=tool, action=check.action,
        key_hash=check.key_hash,
    )
    if verdict == "deny":
        _event(ctx, "approval_denied", {"tool": tool, "action": check.action,
                                        "reason": "grant deny"})
        return ApprovalOutcome(False, "denied", "denegado por un grant previo",
                               action=check.action)
    if verdict == "allow":
        _event(ctx, "approval_granted", {"tool": tool, "action": check.action,
                                         "reason": "grant"})
        return ApprovalOutcome(True, "allow", "grant previo", action=check.action)

    policy = (ctx.policy if ctx else "assisted") or "assisted"
    if policy == "auto":
        return ApprovalOutcome(True, "allow", "policy=auto", action=check.action)
    if policy == "never" or ctx is None or not ctx.interactive:
        reason = ("sin canal interactivo: fail-closed (policy=never)"
                  if policy == "never" else
                  "sin canal interactivo para aprobar: fail-closed")
        _event(ctx, "approval_denied", {"tool": tool, "action": check.action,
                                        "reason": reason})
        return ApprovalOutcome(False, "denied", reason, action=check.action)

    item = approvals.propose(
        session_id, tool, {}, preview=check.preview,
        action=check.action, key_hash=check.key_hash,
        kind="tool", source=(ctx.source if ctx else "chat"),
        timeout_s=float(ctx.timeout_s or 0),
        metadata={"reason": check.reason},
    )
    _event(ctx, "approval_requested", {"tool": tool, "action": check.action,
                                       "request_id": item["id"],
                                       "reason": check.reason})
    try:
        resolved = await approvals.wait(item["id"], float(ctx.timeout_s or 0))
    except Exception as e:
        logger.warning("approval wait fallo: %s", e)
        resolved = approvals.get(item["id"])
    status = (resolved or {}).get("status", "error")
    if status == "approved":
        # Consumir el grant `once` que guardo resolve() (session/always quedan).
        try:
            approvals.check_grant(session_id=session_id, tool=tool,
                                  action=check.action, key_hash=check.key_hash)
        except Exception:
            pass
        _event(ctx, "approval_resolved", {"tool": tool, "action": check.action,
                                          "request_id": item["id"],
                                          "status": "approved"})
        return ApprovalOutcome(True, "allow", "aprobado por el usuario",
                               request_id=item["id"], action=check.action)
    clean = "timed_out" if status in ("timed_out", "pending") else "denied"
    _event(ctx, "approval_resolved", {"tool": tool, "action": check.action,
                                      "request_id": item["id"], "status": clean})
    return ApprovalOutcome(False, clean,
                           "aprobacion rechazada" if clean == "denied"
                           else "aprobacion expirada (fail-closed)",
                           request_id=item["id"], action=check.action)


def build_context(
    *,
    session_id: str = "",
    source: str = "chat",
    interactive: bool = False,
    policy: str | None = None,
    config: dict | None = None,
    event_store: Any = None,
    auto_apply: str | None = None,
    parent_session_id: str = "",
) -> ApprovalContext:
    """Construye el contexto desde config. `policy` explicito gana."""
    cfg = config or {}
    acfg = (cfg.get("approval") or {}) if isinstance(cfg, dict) else {}
    mode = str(policy or acfg.get("mode") or "assisted").lower()
    if mode not in ("auto", "assisted", "strict", "never"):
        mode = "assisted"
    return ApprovalContext(
        session_id=session_id or "",
        source=source,
        interactive=bool(interactive),
        policy=mode,
        timeout_s=float(acfg.get("timeout_s", 120) or 0),
        auto_apply=str(auto_apply or (cfg.get("agent_loop") or {}).get("auto_apply", "assisted")),
        config=cfg,
        event_store=event_store,
        parent_session_id=parent_session_id or "",
    )
