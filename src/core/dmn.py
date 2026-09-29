"""Default Mode Network (DMN) — background cognition between user turns.

Port verbatim de lethe/src/lethe/actor/dmn.py adaptado a Nexus sin Actor system.
Usa AgentLoop + LLMGateway + workspace files. No cruza proyectos — copia lógica pura.

DMN corre en rounds periódicos (cada hora por defecto). Entre rounds persiste
estado en workspace/dmn_state.md. Cada round lee estado previo, piensa, actúa,
y guarda nuevo estado. Modo QUICK (2-3 turns) vs DEEP (hasta 10 turns).

Ver lethe/config/prompts/dmn_system.md y dmn_round_message.md para templates.
Integración: se registra como BackgroundWorker en background_workers.WorkerManager
via DMNWorker (trigger SCHEDULED, interval 3600s). También expone DefaultModeNetwork
standalone para uso directo.

Tools disponibles por round: read_file, write_file, edit_file, bash,
list_directory, web_fetch, graph search, memory ops.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — resueltos desde NEXUS_ROOT / workspace
# ---------------------------------------------------------------------------

def _workspace_dir() -> Path:
    root = os.environ.get("NEXUS_ROOT", "") or str(Path(__file__).parents[2])
    ws = Path(root) / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws

DMN_STATE_FILE = str(_workspace_dir() / "dmn_state.md")
IDEAS_FILE = str(_workspace_dir() / "ideas.md")
QUESTIONS_FILE = str(_workspace_dir() / "questions.md")
DMN_RESET_MARKER = str(_workspace_dir() / ".dmn_state_reset_v1")
FORCE_DEEP_EVERY_N_ROUNDS = 4
IDEAS_STALE_HOURS = 3

# ---------------------------------------------------------------------------
# Prompts — templates verbatim de lethe/config/prompts/dmn_system.md
# ---------------------------------------------------------------------------

DMN_SYSTEM_PROMPT_TEMPLATE = """You are the Default Mode Network (DMN) - a persistent background thinking process.

You run in rounds, triggered periodically (every hour). Between rounds, you persist your state to a file. Each round, you read your previous state and continue thinking.

<principal>
{principal_context}
</principal>

<workspace>
Your workspace is at: {workspace}
Key paths:
- {workspace}/dmn_state.md - your persistent state between rounds
- {workspace}/questions.md - reflections and open questions
- {workspace}/ideas.md - creative ideas, observations, proactive suggestions
- {workspace}/projects/ - project notes and plans
- {workspace}/memory/ - memory block files
- {workspace}/tasks/ - task-related files
- {workspace}/data/ - databases and persistent data
Home directory: {home}
</workspace>

<purpose>
You are the subconscious mind of the AI assistant. Your job is to:
1. **Compact core memory** — the MOST IMPORTANT duty. Review memory blocks for stale, completed, or no-longer-relevant content. Move it to archival memory and remove from the block.
2. **Clean up tasks** — mark completed items as done, archive old task context
3. Scan goals and tasks - check todos, reminders, deadlines approaching
4. Reorganize memory - keep memory blocks clean, relevant, well-organized
5. Self-improve - update {workspace}/questions.md with reflections, identify patterns
6. Monitor projects - scan {workspace}/projects/ for stalled work or opportunities
7. Advance principal's goals - proactively work on things that help the principal
8. Generate ideas - write creative ideas to {workspace}/ideas.md
9. Notify cortex - send messages when something needs user attention
</purpose>

<mode>
You operate in two modes:

QUICK MODE (default: 2-3 turns)
- Use when you find nothing interesting or nothing has changed
- Check reminders, scan for urgent items, do a quick compaction pass, update state, terminate

DEEP MODE (up to 10 turns)
- Use when memory blocks are bloated and need significant compaction
- Use when you discover something worth exploring or developing
- Research, write ideas, draft proactive suggestions, think through problems

Decision rule: If memory blocks are growing large or contain stale content, go DEEP for compaction. If nothing interesting changed and memory is clean, go QUICK.
</mode>

<workflow>
Each round:
1. Read {workspace}/dmn_state.md for context
2. Check reminders (provided in round message)
3. **Read memory blocks and check for compaction opportunities**
4. Decide QUICK vs DEEP
5. Execute: compact memory, clean tasks, take action (write/update files as needed)
6. Write updated state to {workspace}/dmn_state.md (include what you compacted)
7. Call terminate(result) with a clear summary
</workflow>

<rules>
- You are NOT user-facing — never send messages directly to user
- Keep state concise (under 50 lines)
- ALWAYS use absolute paths starting with {workspace}/
- Most rounds should be QUICK — but ALWAYS do at least a quick compaction check
- When archiving, preserve enough context that it can be recalled later
</rules>
"""


def get_dmn_system_prompt(principal_context: str = "") -> str:
    """Build DMN system prompt with resolved workspace paths."""
    principal = principal_context.strip() or (
        "You work to advance your principal's goals across projects, well-being, "
        "relationships, and long-term outcomes. Use memory blocks for current context."
    )
    ws = str(_workspace_dir())
    return DMN_SYSTEM_PROMPT_TEMPLATE.format(
        workspace=ws,
        home=os.path.expanduser("~"),
        principal_context=principal,
    )


DMN_ROUND_MESSAGE_TEMPLATE = """[DMN Round - {timestamp}]

{reminders}
{previous_state}
{mode_directive}

Begin your round. Read state, check tasks, reflect, take action, update state.
When done, finish with a concise summary of what you did.
"""


# ---------------------------------------------------------------------------
# DMN — standalone runner (sin Actor, usa AgentLoop si está disponible)
# ---------------------------------------------------------------------------

class DefaultModeNetwork:
    """Persistent background subagent — port verbatim de lethe DefaultModeNetwork.

    Adaptado: en lugar de ActorRegistry+AsyncLLMClient, usa opcional
    llm_gateway (nexus src/core/llm_gateway) o callable llm_factory.
    Si no hay LLM, el round hace solo file-based housekeeping.
    """

    def __init__(
        self,
        llm_gateway: Any = None,
        llm_factory: Optional[Callable] = None,
        get_reminders: Optional[Callable[[], Any]] = None,
        principal_context_provider: Optional[Callable[[], str]] = None,
        model_override: str = "",
        workspace: str | Path | None = None,
    ):
        self.llm_gateway = llm_gateway
        self.llm_factory = llm_factory
        self.get_reminders = get_reminders
        self.principal_context_provider = principal_context_provider
        self.model_override = (model_override or "").strip()
        self.workspace = Path(workspace) if workspace else _workspace_dir()
        self._status: dict = {
            "state": "idle",
            "rounds_total": 0,
            "last_started_at": "",
            "last_completed_at": "",
            "last_mode": "",
            "last_turns": 0,
            "last_forced_deep": False,
            "last_force_reason": "",
            "last_result": "",
            "last_error": "",
        }
        self._round_history: deque[dict] = deque(maxlen=40)
        # Re-resolve paths if workspace override
        if workspace:
            global DMN_STATE_FILE, IDEAS_FILE, QUESTIONS_FILE, DMN_RESET_MARKER
            DMN_STATE_FILE = str(self.workspace / "dmn_state.md")
            IDEAS_FILE = str(self.workspace / "ideas.md")
            QUESTIONS_FILE = str(self.workspace / "questions.md")
            DMN_RESET_MARKER = str(self.workspace / ".dmn_state_reset_v1")

    async def run_round(self) -> Optional[str]:
        """Execute one DMN round. Returns user_notify message or None."""
        round_started_at = datetime.now(timezone.utc)
        timestamp = round_started_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        self._status["state"] = "running"
        self._status["last_started_at"] = round_started_at.isoformat()
        self._status["last_error"] = ""

        # Reminders
        reminders_text = ""
        if self.get_reminders:
            try:
                r = self.get_reminders()
                if asyncio.iscoroutine(r):
                    r = await r
                if r:
                    reminders_text = f"Active reminders:\n{r}\n"
            except Exception as e:
                logger.warning("DMN: get_reminders failed: %s", e)

        # Previous state
        previous_state = ""
        try:
            if os.path.exists(DMN_STATE_FILE):
                content = Path(DMN_STATE_FILE).read_text(encoding="utf-8").strip()
                if content:
                    previous_state = f"Previous round state:\n{content}\n"
        except Exception as e:
            logger.warning("DMN: read state failed: %s", e)

        # Stale reset
        reset_reason = self._stale_state_reason(previous_state)
        if reset_reason:
            reset_body = (
                "# DMN State — Reset Baseline\n"
                f"*Reset at: {timestamp}*\n\n"
                "## Why reset happened\n"
                f"- {reset_reason}\n\n"
                "## Next focus\n"
                "- Re-anchor to current memory blocks and active project files\n"
                "- Produce at least one concrete idea in ideas.md\n"
                "- Update questions.md only if there is a novel pattern\n"
            )
            try:
                Path(DMN_STATE_FILE).write_text(reset_body, encoding="utf-8")
                Path(DMN_RESET_MARKER).write_text(timestamp, encoding="utf-8")
                previous_state = f"Previous round state:\n{reset_body}\n"
                logger.warning("DMN: state reset (%s)", reset_reason)
            except Exception as e:
                logger.warning("DMN: reset write failed: %s", e)

        force_deep, force_reason = self._should_force_deep(previous_state)
        mode_directive = (
            f"[MODE DIRECTIVE] FORCE DEEP MODE THIS ROUND. Reason: {force_reason}. "
            f"Write at least one idea entry to {IDEAS_FILE} and summarize why it matters."
            if force_deep else
            "[MODE DIRECTIVE] Choose QUICK vs DEEP normally based on detected opportunities."
        )

        file_stats_before = self._snapshot_files()

        message = DMN_ROUND_MESSAGE_TEMPLATE.format(
            timestamp=timestamp,
            reminders=reminders_text,
            previous_state=previous_state,
            mode_directive=mode_directive,
        )

        logger.info("DMN round starting (force_deep=%s)", force_deep)

        # Try LLM round if gateway available, else file-only housekeeping
        result = "No LLM — file housekeeping only"
        turns = 1
        user_message = None

        llm = self._resolve_llm()
        if llm is not None:
            try:
                system_prompt = get_dmn_system_prompt(
                    self.principal_context_provider() if self.principal_context_provider else ""
                )
                # Use gateway chat if available
                if hasattr(llm, "chat"):
                    # llm_gateway style: chat(messages, model, system_prompt)
                    try:
                        resp = await llm.chat(
                            messages=[{"role": "user", "content": message}],
                            model=self.model_override or None,
                            system_prompt=system_prompt,
                        )
                        # normalize response
                        if hasattr(resp, "content"):
                            result = resp.content or str(resp)
                        elif isinstance(resp, dict):
                            result = resp.get("content") or resp.get("text") or str(resp)
                        else:
                            result = str(resp)
                        turns = 2
                    except TypeError:
                        # fallback: chat(prompt)
                        resp = await llm.chat(message)
                        result = str(resp)
                        turns = 2
                elif callable(llm):
                    result = await llm(message)
                    result = str(result)
                    turns = 2
            except Exception as e:
                logger.error("DMN LLM error: %s", e)
                self._status["last_error"] = str(e)
                result = f"LLM error: {e}"
        else:
            # File-only: do minimal compaction check
            result = await self._file_only_round(previous_state)

        # Check for user_notify in result (convention: [USER_NOTIFY] prefix)
        if result and "[USER_NOTIFY]" in result:
            # extract notify block
            idx = result.find("[USER_NOTIFY]")
            user_message = result[idx + len("[USER_NOTIFY]"):].strip()[:2000]

        file_stats_after = self._snapshot_files()
        mode = "DEEP" if "DEEP" in result.upper() or turns > 4 else "QUICK"
        touched = self._diff_file_stats(file_stats_before, file_stats_after)
        round_completed_at = datetime.now(timezone.utc)
        duration_seconds = (round_completed_at - round_started_at).total_seconds()
        logger.info(
            "DMN round complete: mode=%s turns=%s forced=%s reason=%s touched=%s duration=%.1fs",
            mode, turns, force_deep, force_reason or "none", touched or "none", duration_seconds,
        )
        logger.info("DMN result: %s", result[:200])

        self._status["rounds_total"] += 1
        self._status["last_completed_at"] = round_completed_at.isoformat()
        self._status["last_mode"] = mode
        self._status["last_turns"] = turns
        self._status["last_forced_deep"] = force_deep
        self._status["last_force_reason"] = force_reason
        self._status["last_result"] = result[:240]
        self._round_history.append({
            "started_at": round_started_at.isoformat(),
            "completed_at": round_completed_at.isoformat(),
            "mode": mode,
            "turns": int(turns),
            "duration_seconds": round(duration_seconds, 2),
            "forced_deep": bool(force_deep),
            "force_reason": force_reason or "",
            "touched": touched or "",
            "user_notify": bool(user_message),
            "error": self._status.get("last_error", ""),
            "result": result[:240],
        })
        if user_message:
            self._status["last_user_notify"] = user_message[:240]
        self._status["state"] = "idle"
        return user_message

    def _resolve_llm(self):
        if self.llm_gateway is not None:
            return self.llm_gateway
        if self.llm_factory is not None:
            try:
                return self.llm_factory()
            except Exception:
                return None
        # Try to auto-resolve nexus llm_gateway singleton
        try:
            from src.core.llm_gateway import LLMGateway
            gw = LLMGateway()
            # only return if it has providers
            if getattr(gw, "_providers", None) or getattr(gw, "providers", None):
                return gw
        except Exception:
            pass
        return None

    async def _file_only_round(self, previous_state: str) -> str:
        """Minimal housekeeping when no LLM — just ensure state files exist."""
        try:
            ws = self.workspace
            # Ensure ideas.md/questions.md exist
            for fname, boilerplate in [
                ("ideas.md", "# Ideas\n\n*DMN will write creative ideas here.*\n"),
                ("questions.md", "# Questions\n\n*DMN reflections and open questions.*\n"),
                ("dmn_state.md", f"# DMN State\n\n*Round 0 — {datetime.now(timezone.utc).isoformat()}*\n- No LLM configured; file housekeeping only.\n"),
            ]:
                p = ws / fname
                if not p.exists():
                    p.write_text(boilerplate, encoding="utf-8")
            return "File housekeeping complete (no LLM)"
        except Exception as e:
            return f"Housekeeping error: {e}"

    # ---- helpers verbatim de lethe ----

    def _extract_round_number(self, previous_state: str) -> int:
        if not previous_state:
            return 0
        m = re.search(r"Round\s+(\d+)", previous_state)
        if not m:
            return 0
        try:
            return int(m.group(1))
        except ValueError:
            return 0

    def _should_force_deep(self, previous_state: str) -> tuple[bool, str]:
        last_round = self._extract_round_number(previous_state)
        if last_round and last_round % FORCE_DEEP_EVERY_N_ROUNDS == 0:
            return True, f"periodic deep cadence (every {FORCE_DEEP_EVERY_N_ROUNDS} rounds)"
        try:
            if os.path.exists(IDEAS_FILE):
                age = datetime.now(timezone.utc).timestamp() - os.path.getmtime(IDEAS_FILE)
                if age > IDEAS_STALE_HOURS * 3600:
                    hours = int(age // 3600)
                    return True, f"ideas.md stale for {hours}h"
        except Exception:
            pass
        return False, ""

    def _stale_state_reason(self, previous_state: str) -> str:
        if not previous_state:
            return ""
        if os.path.exists(DMN_RESET_MARKER):
            return ""
        stale_markers = ("Valentine", "no changes", "all stable")
        marker_hits = sum(1 for m in stale_markers if m.lower() in previous_state.lower())
        quick_hits = previous_state.lower().count("quick check")
        if marker_hits >= 2 or quick_hits >= 4:
            return "state appears repetitive/stale and anchored to old context"
        return ""

    def _snapshot_files(self) -> dict:
        snap = {}
        for path in (DMN_STATE_FILE, IDEAS_FILE, QUESTIONS_FILE):
            try:
                if os.path.exists(path):
                    snap[path] = {"mtime": os.path.getmtime(path), "size": os.path.getsize(path)}
                else:
                    snap[path] = {"mtime": 0, "size": 0}
            except Exception:
                snap[path] = {"mtime": 0, "size": 0}
        return snap

    def _diff_file_stats(self, before: dict, after: dict) -> str:
        changed = []
        for path, info_after in after.items():
            info_before = before.get(path, {"mtime": 0, "size": 0})
            if info_after["mtime"] != info_before["mtime"] or info_after["size"] != info_before["size"]:
                base = os.path.basename(path)
                changed.append(f"{base}({info_before['size']}->{info_after['size']} bytes)")
        return ", ".join(changed)

    @property
    def status(self) -> dict:
        s = dict(self._status)
        s["round_history"] = list(self._round_history)
        return s

    @staticmethod
    def _take_lines(text: str, max_lines: int) -> str:
        if not text:
            return ""
        lines = str(text).splitlines()
        if len(lines) <= max_lines:
            return str(text)
        return "\n".join(lines[:max_lines]) + "\n...[truncated by lines]"

    def get_context_view(self, max_lines: int = 220) -> str:
        try:
            if os.path.exists(DMN_STATE_FILE):
                state_text = Path(DMN_STATE_FILE).read_text(encoding="utf-8")
            else:
                state_text = "(dmn_state.md not found)"
        except Exception as e:
            state_text = f"(failed to read dmn_state.md: {e})"
        principal = ""
        if self.principal_context_provider:
            try:
                principal = self.principal_context_provider() or ""
            except Exception as e:
                principal = f"(failed: {e})"
        s = self.status
        lines = [
            "# DMN Context",
            "",
            f"- state: {s.get('state', 'idle')}",
            f"- rounds_total: {s.get('rounds_total', 0)}",
            f"- last_mode: {s.get('last_mode', '-')}",
            f"- last_turns: {s.get('last_turns', 0)}",
            f"- last_started_at: {s.get('last_started_at') or '-'}",
            f"- last_completed_at: {s.get('last_completed_at') or '-'}",
            f"- last_error: {s.get('last_error') or '-'}",
            "",
            "## Principal context snapshot",
            self._take_lines(principal, max_lines=80) or "(none)",
            "",
            "## dmn_state.md",
            self._take_lines(state_text, max_lines=max_lines) if state_text else "(empty)",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# BackgroundWorker adaptador — se registra en WorkerManager
# ---------------------------------------------------------------------------

def make_dmn_worker(dmn: DefaultModeNetwork | None = None):
    """Factory para WorkerManager: crea DMNWorker (SCHEDULED 3600s)."""
    from src.core.background_workers import BaseWorker, WorkerConfig, WorkerTrigger, WorkerResult

    _dmn = dmn or DefaultModeNetwork()

    class DMNWorker(BaseWorker):
        """DMN como BackgroundWorker — expone rounds en el loop de workers."""

        def __init__(self):
            super().__init__(WorkerConfig(
                name="dmn_background_cognition",
                trigger=WorkerTrigger.SCHEDULED,
                interval_seconds=3600,
                timeout_seconds=600,
                enabled=True,
            ))
            self.dmn = _dmn

        async def run(self, context: dict) -> WorkerResult:
            try:
                # Inyectar llm_gateway del contexto si DMN no tiene uno
                if self.dmn.llm_gateway is None and context.get("llm_gateway"):
                    self.dmn.llm_gateway = context["llm_gateway"]
                # Reminders desde context si hay
                if self.dmn.get_reminders is None and context.get("get_reminders"):
                    self.dmn.get_reminders = context["get_reminders"]
                notify = await self.dmn.run_round()
                data: dict[str, Any] = {
                    "dmn_status": self.dmn.status,
                    "touched": self.dmn.status.get("last_result", "")[:200],
                }
                if notify:
                    data["user_notify"] = notify
                    # Emitir al event bus si existe
                    try:
                        from src.observability.event_stream import emit, EventType
                        emit(EventType.DMN_NOTIFY, data={"message": notify}, source="dmn")
                    except Exception:
                        pass
                return WorkerResult(
                    worker_name=self.config.name,
                    success=True,
                    duration_ms=0,
                    data=data,
                )
            except Exception as e:
                logger.error("DMN worker error: %s", e, exc_info=True)
                return WorkerResult(self.config.name, success=False, duration_ms=0, error=str(e))

    return DMNWorker()
