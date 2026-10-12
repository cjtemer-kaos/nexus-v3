"""
slash_commands — UI-agnostic slash-command registry.

Pattern (openakita /persona /model /skill ...): a single place that defines
every "/foo bar" command, what it does, and what it returns. The same
registry powers chat input parsing, the help palette, the CLI, and
(future) voice command parsing.

Why server-side instead of per-UI: avoids 3 implementations of the same
8 commands drifting. UI just renders what `commands()` returns and
forwards execution to `execute()`.

Commands ship as namespaced dotted strings ("model", "persona", "skill",
"clear", "help", "plan", "agent", "session", "doctor"). Each command has:
  name, alias[], description, args (schema text), execute(args, ctx) -> dict
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SlashResult:
    ok: bool
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "data": self.data}


SlashHandler = Callable[[List[str], Dict[str, Any]], Awaitable[SlashResult]]


@dataclass
class SlashCommand:
    name: str
    aliases: List[str]
    description: str
    args: str            # human-readable hint, e.g. "<model>" or "[gema]"
    handler: SlashHandler

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "args": self.args,
        }


class SlashRegistry:
    def __init__(self):
        self._commands: Dict[str, SlashCommand] = {}

    def register(self, cmd: SlashCommand) -> None:
        self._commands[cmd.name] = cmd
        for a in cmd.aliases:
            self._commands[a] = cmd

    def get(self, name: str) -> Optional[SlashCommand]:
        return self._commands.get(name.lstrip("/").lower())

    def list(self) -> List[SlashCommand]:
        # dedupe (a command can be reached by name + multiple aliases)
        seen = set()
        out: List[SlashCommand] = []
        for cmd in self._commands.values():
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            out.append(cmd)
        return sorted(out, key=lambda c: c.name)

    async def execute(
        self, raw: str, ctx: Optional[Dict[str, Any]] = None
    ) -> SlashResult:
        """Parse `raw` ("/persona jarvis") and dispatch.
        Returns SlashResult — never raises (caller decides what to render)."""
        ctx = ctx or {}
        if not raw or not raw.strip():
            return SlashResult(ok=False, message="empty command")
        parts = raw.strip().split()
        head = parts[0].lstrip("/").lower()
        args = parts[1:]
        cmd = self._commands.get(head)
        if cmd is None:
            return SlashResult(
                ok=False,
                message=f"unknown slash command: /{head}",
                data={"available": [c.name for c in self.list()]},
            )
        try:
            return await cmd.handler(args, ctx)
        except Exception as e:
            logger.error(f"slash /{head} handler crashed: {e}")
            return SlashResult(ok=False, message=f"/{head} error: {type(e).__name__}: {e}")


# Module singleton + default registrations
registry = SlashRegistry()


# --- default handlers --------------------------------------------------

async def _h_help(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    cmds = registry.list()
    return SlashResult(
        ok=True,
        message="\n".join(f"/{c.name} {c.args} — {c.description}" for c in cmds),
        data={"commands": [c.to_dict() for c in cmds]},
    )


async def _h_clear(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    # Signals to the caller (UI) to reset its local chat buffer. Server-side
    # session memory is intentionally NOT wiped here — that's a separate
    # explicit action and would be destructive.
    return SlashResult(ok=True, message="local chat cleared (server state untouched)",
                       data={"action": "clear_local_only"})


async def _h_model(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    if not args:
        return SlashResult(ok=False, message="usage: /model <model_id>",
                           data={"action": "list_models"})
    model = args[0]
    return SlashResult(ok=True, message=f"model preference set to {model}",
                       data={"action": "set_model", "model": model})


async def _h_persona(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    presets = ["default", "business", "tech_expert", "butler",
               "girlfriend", "boyfriend", "family"]
    if not args:
        return SlashResult(ok=True,
                           message=f"available personas: {', '.join(presets)}",
                           data={"action": "list_personas", "presets": presets})
    persona = args[0].lower()
    if persona not in presets:
        return SlashResult(ok=False,
                           message=f"unknown persona: {persona}",
                           data={"presets": presets})
    return SlashResult(ok=True, message=f"persona set to {persona}",
                       data={"action": "set_persona", "persona": persona})


async def _h_skill(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    if not args:
        return SlashResult(ok=False, message="usage: /skill <name>",
                           data={"action": "list_skills"})
    return SlashResult(ok=True, message=f"skill {args[0]} requested",
                       data={"action": "invoke_skill", "skill": args[0]})


async def _h_agent(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    if not args:
        return SlashResult(ok=False, message="usage: /agent <name>",
                           data={"action": "list_agents"})
    return SlashResult(ok=True, message=f"switch to agent {args[0]}",
                       data={"action": "set_agent", "agent": args[0]})


async def _h_session(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    # /session new | /session attach <id> | /session list
    sub = args[0].lower() if args else "current"
    if sub == "new":
        import uuid
        sid = f"sess_{uuid.uuid4().hex[:10]}"
        return SlashResult(ok=True, message=f"new session id: {sid}",
                           data={"action": "session_new", "session_id": sid})
    if sub == "attach":
        if len(args) < 2:
            return SlashResult(ok=False, message="usage: /session attach <id>")
        return SlashResult(ok=True, message=f"attach to {args[1]}",
                           data={"action": "session_attach", "session_id": args[1]})
    if sub == "list":
        return SlashResult(ok=True, message="see GET /api/sessions/catalog",
                           data={"action": "session_list", "endpoint": "/api/sessions/catalog"})
    return SlashResult(ok=True,
                       message=f"current session: {ctx.get('session_id') or '(none)'}",
                       data={"action": "session_current", "session_id": ctx.get('session_id')})


async def _h_plan(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    toggle = args[0].lower() if args else "toggle"
    on = toggle in ("on", "true", "1", "yes")
    return SlashResult(ok=True, message=f"plan mode {'on' if on else 'off/toggle'}",
                       data={"action": "set_plan_mode", "on": on if args else None})


async def _h_doctor(args: List[str], ctx: Dict[str, Any]) -> SlashResult:
    return SlashResult(ok=True, message="run GET /api/doctor for live diagnostic",
                       data={"action": "open", "endpoint": "/api/doctor"})



async def _h_compact(args, ctx):
    return SlashResult(ok=True, message="compactación 4 capas: L0 SecurityLingua filtra, L1 recorte 4 turnos, L2 micro colapsa >2k, L3 spill ./data/spill, L4 resumen barato. Emergencia prompt_too_long -> compactación reactiva.",
                       data={"action": "compact_info"})

async def _h_goal(args, ctx):
    if not args:
        return SlashResult(ok=True, message="uso: /goal <objetivo largo> | /goal status | /goal clear — crea objetivo persistente para rondas autónomas",
                           data={"action": "goal_help"})
    sub = args[0].lower()
    if sub in ("status", "show", "get"):
        return SlashResult(ok=True, message="goal actual en session goals (ver GET /api/sessions/{id})",
                           data={"action": "goal_status"})
    if sub == "clear":
        return SlashResult(ok=True, message="goal borrado para esta sesión", data={"action": "goal_clear"})
    objective = " ".join(args)
    if len(objective) < 8:
        return SlashResult(ok=False, message="objetivo muy corto (min 8 chars)")
    return SlashResult(ok=True, message=f"goal registrado: {objective[:120]}", data={"action": "goal_set", "objective": objective})

async def _h_permission(args, ctx):
    opts = ["read_only", "workspace_write", "full"]
    if not args:
        return SlashResult(ok=True, message=f"permisos: {', '.join(opts)} — uso: /permission <{('|').join(opts)}>",
                           data={"action": "permission_list", "options": opts})
    v = args[0].lower().replace("-", "_")
    # alias friendly
    alias = {"read": "read_only", "readonly": "read_only", "workspace": "workspace_write", "write": "workspace_write", "full_access": "full"}
    v = alias.get(v, v)
    if v not in opts:
        return SlashResult(ok=False, message=f"permiso inválido {v!r} — opciones: {', '.join(opts)}")
    return SlashResult(ok=True, message=f"permiso -> {v}", data={"action": "set_permission", "permission": v})

async def _h_export(args, ctx):
    return SlashResult(ok=True, message="export: descarga ZIP del log de sesión (GET /api/sessions/{id}/export)",
                       data={"action": "export_session", "endpoint": "/api/sessions/{id}/export"})

async def _h_feedback(args, ctx):
    msg = " ".join(args) if args else ""
    if not msg:
        return SlashResult(ok=True, message="feedback: envía comentario — uso: /feedback <texto>", data={"action": "feedback_help"})
    return SlashResult(ok=True, message=f"feedback recibido: {msg[:200]}", data={"action": "feedback", "text": msg})

async def _h_todo(args, ctx):
    return SlashResult(ok=True, message="todo_write es tool del modelo; gestionado vía POST /api/todos. Uso slash: /todo para ver lista actual.",
                       data={"action": "todo_list"})


async def _h_tools(args, ctx):
    tools = [
        ("read_file", "lee un archivo (jail/permiso)"),
        ("write_file", "escribe/crea un archivo"),
        ("list_dir", "lista un directorio"),
        ("exec", "ejecuta un comando de shell"),
        ("todo_write", "lista de tareas estructurada"),
        ("web_fetch", "descarga una URL"),
        ("learn", "guarda conocimiento en el grafo"),
        ("recall", "consulta el grafo de conocimiento"),
        ("mcp_list", "lista tools de servidores MCP"),
        ("mcp_call", "llama una tool MCP"),
        ("gema_list", "lista las gemas especializadas"),
        ("agent_list", "lista los ayudantes configurados"),
        ("gema_run", "delega una tarea a una gema"),
        ("agent_run", "delega una tarea a un ayudante"),
        ("echo", "eco (debug)"),
    ]
    return SlashResult(ok=True,
                       message=f"Herramientas ({len(tools)}):\n" + "\n".join(f"  {n} — {d}" for n, d in tools),
                       data={"action": "list_tools", "tools": [n for n, _ in tools]})


async def _h_scope(args, ctx):
    return SlashResult(
        ok=True,
        message=("Alcance: con permiso 'full' (Ajustes → Permisos) hay acceso a toda la PC; "
                 "con 'workspace_write' solo ./workspace y ./data; con 'read_only' solo lectura. "
                 "Secretos **/.env y **/*.key siempre bloqueados. Cambiá con /permiso <read_only|workspace_write|full>."),
        data={"action": "scope_info"},
    )


# --- register defaults -------------------------------------------------

for spec in (
    SlashCommand("help",    ["h", "?", "ayuda"],     "show available commands",        "",            _h_help),
    SlashCommand("clear",   ["cls", "limpiar"],      "clear local chat buffer",        "",            _h_clear),
    SlashCommand("model",   ["m", "modelo"],         "set model preference",           "<model_id>",  _h_model),
    SlashCommand("persona", ["p"],                   "switch persona preset",          "[preset]",    _h_persona),
    SlashCommand("skill",   ["s"],                   "invoke a skill by name",         "<name>",      _h_skill),
    SlashCommand("agent",   ["a", "ayudante", "ayudantes"], "switch to an agent",      "<name>",      _h_agent),
    SlashCommand("session", ["sess", "sesion"],      "session ops (new/attach/list)",  "[op] [id]",   _h_session),
    SlashCommand("plan",    ["planear"],             "toggle plan mode",               "[on|off]",    _h_plan),
    SlashCommand("doctor",  ["dr"],                  "open the diagnostic endpoint",   "",            _h_doctor),
    SlashCommand("compact",   ["compactar"],         "info compactación 4 capas",      "",            _h_compact),
    SlashCommand("goal",      ["g", "objetivo"],     "gestiona objetivo largo (goal)", "[objetivo|status|clear]", _h_goal),
    SlashCommand("permission", ["perm", "permiso"],  "cambia permiso (read_only|workspace_write|full)", "<perm>", _h_permission),
    SlashCommand("export",    ["exportar"],          "exporta log de sesión ZIP",      "",            _h_export),
    SlashCommand("feedback",  ["fb"],                "envía feedback",               "[texto]",     _h_feedback),
    SlashCommand("todo",      ["todos"],             "ver lista todo actual",          "",            _h_todo),
    SlashCommand("herramientas", ["tools", "herramienta"], "lista las herramientas disponibles", "", _h_tools),
    SlashCommand("alcance",   ["scope"],             "info de alcance/permisos",       "",            _h_scope),
):
    registry.register(spec)
