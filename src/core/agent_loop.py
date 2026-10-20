"""Bucle Agente — TDAO (Think-Decide-Act-Observe) + budget + error classifier + heartbeat.

Origen: SuperNEXUS agent_loop.py (TDAO + ErrorAction) + Hermes conversation_loop (3900 LOC) + iteration_budget + error_classifier + cordis hooks.
Ver BLUEPRINT.md §6.

Flow per iteration:
  1. THINK  — LLM call via ProviderRouter (with fallback)
  2. DECIDE — parse tool_calls vs respuesta final (content)
  3. ACT    — scope.guard + hooks.pre_tool_use + tool_registry.dispatch + hooks.post_tool_use
  4. OBSERVE — append to SessionLog + EventStore + Trajectory, classify error, check budget, maybe compact

Integration:
  - SessionLog (Fase 0) for persistent message history
  - EventStore for replay/audit
  - CompactionPipeline for context window management
  - HooksEngine for middleware
  - ToolRegistry for unified dispatch
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .provider_base import LLMMessage, ToolCall
from .iteration_budget import IterationBudget
from .error_classifier import ErrorAction, classify_error
from .trajectory import Trajectory, TrajectoryStep

import os as _os

# Herramientas de SOLO LECTURA: seguras de ejecutar en paralelo (sin efectos).
_READONLY_TOOLS = {
    "read", "read_file", "list", "ls", "list_dir", "glob", "grep", "search",
    "code_search", "recall", "memory_recall", "brain_recall", "search_observations",
    "web_fetch", "web_search", "fetch", "mcp_list", "gema_list", "agent_list",
    "get_system_info", "system_resources", "status", "graph", "knowledge_graph",
    "fts_search", "find_related", "get_entities", "latest_topic", "find_path",
}


def _parallel_tools_on() -> bool:
    return _os.environ.get("NEXUS_PARALLEL_TOOLS", "1").lower() not in ("0", "false", "no")

logger = logging.getLogger("nexus-agent-loop")

# Re-export for callers
__all__ = ["ErrorAction", "ToolResult", "LoopStep", "LoopResult", "IterationBudget", "AgentLoop"]


@dataclass(frozen=True, slots=True)
class ToolResult:
    success: bool
    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LoopStep:
    iteration: int
    phase: str  # think|decide|act|observe
    content: str
    tool_used: str | None = None
    result: ToolResult | None = None
    # --- PASO 1: schema unificado con TrajectoryStep ---
    turn: int = 0
    seq: int = 0
    tokens: int = 0
    model: str = ""
    duration_ms: float = 0.0
    end_reason: str = ""

    def to_trajectory_step(self) -> TrajectoryStep:
        """Convierte al schema unificado de trayectoria."""
        from .trajectory import TrajectoryStep as _TS
        return _TS(
            iteration=self.iteration,
            role="tool" if self.tool_used and self.phase == "act" else ("assistant" if self.phase in ("decide", "think") else "event"),
            content=self.content,
            tool_name=self.tool_used,
            turn=self.turn or self.iteration,
            step=self.phase,
            seq=self.seq,
            duration_ms=max(0.0, float(self.duration_ms or 0.0)),
            tokens=self.tokens,
            model=self.model,
            end_reason=self.end_reason,
        )


@dataclass
class LoopResult:
    task: str
    success: bool
    final_output: str
    steps: list[LoopStep]
    iterations: int
    total_tokens: int = 0
    duration_ms: float = 0
    trajectory: Trajectory | None = None
    compact_metrics: Any | None = None

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "success": self.success,
            "final_output": self.final_output,
            "iterations": self.iterations,
            "total_tokens": self.total_tokens,
            "duration_ms": round(self.duration_ms, 1),
            "steps": [
                {"iteration": s.iteration, "phase": s.phase, "content": s.content[:2000], "tool_used": s.tool_used, "result": s.result.output[:2000] if s.result else None}
                for s in self.steps
            ],
        }


class AgentLoop:
    """TDAO loop. ProviderRouter + ToolRegistry + Scope + Hooks + SessionLog + EventStore."""

    def __init__(
        self,
        provider_router=None,
        tool_registry=None,
        scope=None,
        hooks=None,
        budget: IterationBudget | None = None,
        session_log=None,
        event_store=None,
        compaction=None,
        distill_hook=None,
        max_iterations: int = 25,
        heartbeat_ms: int = 2000,
        context_max_tokens: int = 32000,
        compact_threshold: float = 0.75,
    ):
        self.router = provider_router
        self.tools = tool_registry
        self.scope = scope
        self.hooks = hooks
        self.budget = budget or IterationBudget(max_iterations=max_iterations)
        self.session_log = session_log
        self.event_store = event_store
        self.compaction = compaction
        self.distill_hook = distill_hook
        self.heartbeat_ms = heartbeat_ms
        # Presupuesto de contexto y umbral de compactación (config.session.*).
        # Antes estaban fijos en 32000/0.75 y la config no tenía efecto.
        self.context_max_tokens = int(context_max_tokens or 32000)
        self.compact_threshold = float(compact_threshold or 0.75)

        # Per-task retry/replan counters
        self._retry_counts: dict[str, int] = {}
        self._replan_counts: dict[str, int] = {}

    async def _distill(self, session_id: str) -> None:
        """Destila la sesión al grafo ANTES de compactar (no perder conocimiento)."""
        if self.distill_hook is None:
            return
        try:
            res = self.distill_hook(session_id)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            logger.debug(f"distill hook falló: {e}")

    def _extract_text_tool_calls(self, content: str) -> list:
        """Respaldo para modelos que escriben JSON en texto en vez de tool_calls.

        Acepta {"tool": "<nombre>", "arguments": {...}} o
        {"tool": "<nombre>", "parameters": {...}} o el resto de claves como args.
        Solo acepta herramientas registradas (self.tools) para no ejecutar basura.
        """
        out: list = []
        if not content:
            return out
        # Nota Fase 8: antes habia `if "{" not in content` aqui, lo que cortaba
        # el escaneo 0 (nombre("texto")) — esa forma no lleva llaves. El escaneo
        # 1 (JSON balanceado) solo activa si hay '{', asi que el gate global
        # sobraba.
        try:
            names = set()
            if self.tools is not None and hasattr(self.tools, "list"):
                try:
                    names = {getattr(t, "name", "") for t in self.tools.list()}
                except Exception:
                    names = set()
        except Exception:
            names = set()
        # Escaneo 0 (sintaxis nombre("texto")) SOLO fuera de bloques de codigo:
        # un ejemplo ilustrativo echo("hola") dentro de ``` o `...` no debe
        # despacharse como tool real (capa 7, ejecucion alucinada). El escaneo 1
        # (bloques JSON balanceados) si busca dentro de fences a proposito:
        # los modelos locales emiten {"tool": ...} justo ahi.
        import re as _re2
        _scan0 = _re2.sub(r"```.*?```", " ", content, flags=_re2.DOTALL)
        _scan0 = _re2.sub(r"`[^`\n]+`", " ", _scan0)
        schemas: dict[str, dict] = {}
        if self.tools is not None and hasattr(self.tools, "_tools"):
            try:
                for _tn, _te in self.tools._tools.items():
                    _p = getattr(_te, "parameters", {}) or {}
                    schemas[_tn] = _p
            except Exception:
                pass
        for _m in _re2.finditer(r"(?i)(?:^|[\s:>`\"'(\[])([a-zA-Z_][a-zA-Z0-9_\.]*)\(\s*\"([^\"]{1,500})\"\s*\)", _scan0):
            _raw = _m.group(1)
            _txt = _m.group(2)
            _cand = _raw[5:] if _raw.lower().startswith("tool_") else _raw
            _match = None
            if _cand in names or _cand in schemas:
                _match = _cand
            else:
                for _n in list(names) + list(schemas.keys()):
                    if _n and _n.lower() == _cand.lower():
                        _match = _n
                        break
            if not _match:
                continue
            _props = {}
            try:
                _props = (schemas.get(_match, {}) or {}).get("properties", {}) or {}
            except Exception:
                _props = {}
            _argname = next(iter(_props.keys()), "text")
            try:
                out.append(ToolCall(id=f"text-{len(out)}", name=_match, arguments={_argname: _txt}))
            except Exception:
                continue
            if len(out) >= 5:
                return out
        # 1) Escáner de bloques JSON balanceados (soporta objetos anidados)
        starts = [i for i, ch in enumerate(content) if ch == "{"]
        for si in starts:
            depth = 0
            in_str = False
            esc = False
            end = -1
            for i in range(si, min(si + 4000, len(content))):
                ch = content[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end < 0:
                continue
            try:
                obj = json.loads(content[si:end])
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            name = str(obj.get("tool", ""))
            if not name:
                # Forma OpenAI suelta: {"name": "<tool>", "arguments": {...}}
                _nm = str(obj.get("name", ""))
                if _nm and ("arguments" in obj or "parameters" in obj):
                    name = _nm
            if not name:
                continue
            if names and name not in names:
                continue
            args = obj.get("arguments")
            if not isinstance(args, dict):
                args = obj.get("parameters")
            if not isinstance(args, dict):
                args = {k: v for k, v in obj.items() if k != "tool"}
            try:
                out.append(ToolCall(id=f"text-{len(out)}", name=name, arguments=args if isinstance(args, dict) else {}))
            except Exception:
                continue
            if len(out) >= 5:
                break
        return out

    def _emit_cycle(self, trajectory: Trajectory, kind: str, iteration: int, content: str = "", duration_ms: float = 0.0, end_reason: str = "", tokens: int = 0, model: str = "") -> None:
        """Emite un evento del ciclo turn/step a trajectory + event_store + hooks (best-effort)."""
        try:
            trajectory.append_event(kind, content=content, iteration=iteration, turn=iteration, duration_ms=max(0.0, float(duration_ms or 0.0)), end_reason=end_reason, tokens=tokens, model=model)
        except Exception:
            pass
        if self.event_store is not None:
            try:
                self.event_store.append_event(kind, {"iteration": iteration, "turn": iteration, "content": (content or "")[:2000], "duration_ms": max(0.0, float(duration_ms or 0.0)), "end_reason": end_reason, "tokens": tokens, "model": model}, session_id=trajectory.session_id)
            except Exception:
                pass

    async def _emit_cycle_async(self, trajectory: Trajectory, kind: str, iteration: int, content: str = "", duration_ms: float = 0.0, end_reason: str = "", tokens: int = 0, model: str = "") -> None:
        self._emit_cycle(trajectory, kind, iteration, content, duration_ms, end_reason, tokens, model)
        if self.hooks is not None:
            try:
                res = self.hooks.emit(kind, {"iteration": iteration, "turn": iteration, "session_id": trajectory.session_id})
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

    async def _stream_turn(self, messages, tools, hint, force_tools, require_vision: bool = False) -> Any:
        """Consume router.stream, reenvia deltas de texto via on_delta, devuelve la respuesta final."""
        final = None
        async for ev in self.router.stream(
            messages, tools=tools, hint=hint, force_tools=force_tools,
            model=getattr(self, "_req_model", None), require_vision=require_vision,
        ):
            et = ev.get("type") if isinstance(ev, dict) else None
            if et == "text":
                try:
                    _r = self._on_delta(ev.get("text", ""))
                    if asyncio.iscoroutine(_r):
                        await _r
                except Exception:
                    pass
            elif et == "reasoning":
                _onr = getattr(self, "_on_reasoning", None)
                if _onr is not None:
                    try:
                        _r = _onr(ev.get("text", ""))
                        if asyncio.iscoroutine(_r):
                            await _r
                    except Exception:
                        pass
            elif et == "final":
                final = ev.get("response")
        if final is None:
            raise RuntimeError("stream sin respuesta final")
        return final

    @staticmethod
    def _rebuild_messages(dict_msgs) -> list:
        """Reconstruye LLMMessage preservando tool_calls/tool_call_id/images.
        Sin esto, la compactacion produce un payload OpenAI invalido (tool sin parent)."""
        out: list = []
        for d in dict_msgs:
            if not isinstance(d, dict):
                continue
            kwargs: dict = {}
            tcs = d.get("tool_calls")
            if tcs:
                _parsed = []
                for tc in tcs:
                    if isinstance(tc, ToolCall):
                        _parsed.append(tc)
                    elif isinstance(tc, dict):
                        _fn = tc.get("function") or {}
                        _parsed.append(ToolCall(
                            id=tc.get("id", "") or "",
                            name=_fn.get("name") or tc.get("name", "") or "",
                            arguments=_fn.get("arguments") or tc.get("arguments") or {},
                        ))
                if _parsed:
                    kwargs["tool_calls"] = tuple(_parsed)
            if d.get("tool_call_id"):
                kwargs["tool_call_id"] = d.get("tool_call_id")
            if d.get("images"):
                kwargs["images"] = tuple(d.get("images"))
            out.append(LLMMessage(role=d.get("role", "user"), content=d.get("content"), **kwargs))
        return out

    @staticmethod
    def _to_dicts(messages) -> list:
        """Inverso de _rebuild_messages: dicts que PRESERVAN tool_calls/tool_call_id/images.
        Sin esto la compactacion (que consume dicts) descarta los metadatos ANTES de reconstruir."""
        out: list = []
        for m in messages:
            d: dict = {"role": getattr(m, "role", "user"), "content": getattr(m, "content", None) or ""}
            tcs = getattr(m, "tool_calls", ()) or ()
            if tcs:
                d["tool_calls"] = [
                    {"id": getattr(tc, "id", ""), "type": "function",
                     "function": {"name": getattr(tc, "name", ""), "arguments": getattr(tc, "arguments", {})}}
                    for tc in tcs
                ]
            _tcid = getattr(m, "tool_call_id", "")
            if _tcid:
                d["tool_call_id"] = _tcid
            _imgs = getattr(m, "images", None)
            if _imgs:
                d["images"] = list(_imgs)
            out.append(d)
        return out

    def _synthesize_from_tools(self, messages) -> str:
        """Fallback cuando el modelo no da respuesta final: resume el último resultado de tool.

        Evita declarar 'éxito' con output vacío (bug de 'reply vacío').
        """
        last = ""
        for m in reversed(messages):
            if getattr(m, "role", "") == "tool" and (getattr(m, "content", "") or "").strip():
                last = (m.content or "").strip()
                break
        if not last:
            return ""
        if len(last) > 1500:
            last = last[:1500] + "\n…[truncado]"
        return "Listo (respuesta sintetizada de las herramientas ejecutadas):\n" + last

    async def run(self, task: str, session_id: str, hint: str | None = None, system_prompt: str | None = None, force_tools: bool = False, model: str | None = None, images: list[str] | None = None, trajectory: Trajectory | None = None, plan_mode=None, on_delta=None, on_reasoning=None, steer=None, extra_system: str | None = None) -> LoopResult:
        """Run TDAO loop until done, ABORT, or budget exhausted.

        Args:
            task: user task / prompt
            session_id: SessionLog session id
            hint: provider hint (simple|coding|reasoning|deep etc)
            system_prompt: optional system prompt override
            images: optional vision attachments (data URIs) on the user message
        """
        t0 = time.monotonic()
        steps: list[LoopStep] = []
        trajectory = trajectory or Trajectory(session_id=session_id, task=task)
        # Resume: cerrar turnos interrumpidos antes de emitir nada nuevo.
        try:
            trajectory.close_interrupted_turns()
        except Exception:
            pass
        total_tokens = 0
        success = False
        final_output = ""
        self._empty_retries = 0  # type: ignore[attr-defined]
        self._reason_retries = 0  # type: ignore[attr-defined]
        self._retry_counts.clear()
        self._replan_counts.clear()
        self._consecutive_failures = 0  # type: ignore[attr-defined]
        self._tool_fp_window = []  # type: ignore[attr-defined]
        self._tool_repeat = {}  # type: ignore[attr-defined]
        try:
            if self.budget is not None:
                self.budget.reset()  # reset per-run (budget persiste en instancias reusadas)
        except Exception:
            pass
        self._req_model = model  # type: ignore[attr-defined]
        self._on_delta = on_delta  # type: ignore[attr-defined]
        self._on_reasoning = on_reasoning  # type: ignore[attr-defined]
        # P0 prompt-cache stickiness: fija el modelo UNA vez por run (si no se pidió uno)
        # para no invalidar el prompt/KV cache cambiando de modelo entre turnos.
        if (self._req_model is None and getattr(self, "router", None) is not None
                and _os.environ.get("NEXUS_STICKY_MODEL", "1").lower() not in ("0", "false", "no")):
            try:
                _routed = self.router.resolve(task_hint=hint)
                if _routed is not None and getattr(_routed, "model", None):
                    self._req_model = _routed.model
            except Exception:
                pass
        self._tool_fp_window: list[tuple[str, str]] = []  # type: ignore[attr-defined]

        # Init trajectory + session
        trajectory.append_user(task, iteration=0)
        if self.session_log is not None:
            try:
                from .session_log import Message
                self.session_log.append(session_id, Message(role="user", content=task))
            except Exception as e:
                logger.debug(f"session append user falló: {e}")
        if self.event_store is not None:
            try:
                self.event_store.append_event("user_message", {"content": task}, session_id=session_id)
            except Exception:
                pass
        if self.hooks is not None:
            try:
                await self.hooks.emit("session_start", {"session_id": session_id, "task": task, "hint": hint})
            except Exception:
                pass

        # Build initial messages
        messages: list[LLMMessage] = []
        if system_prompt:
            messages.append(LLMMessage(role="system", content=system_prompt))
        else:
            messages.append(LLMMessage(role="system", content=(
                "Eres Nexus v3, un agente con herramientas reales. Usa tool_calls SOLO "
                "si la tarea requiere acceder a archivos, sistema, memoria o ejecutar algo. "
                "Antes de responder sobre decisiones previas, preferencias, lecciones o "
                "historial del proyecto, usa recall para consultar el grafo local. Si recall "
                "devuelve datos concretos (parámetros, rutas, versiones, pasos, nombres), usá "
                "EXACTAMENTE esos valores y citá el nodo; NUNCA inventes valores técnicos. "
                "Después "
                "de aprender algo duradero, usa learn para persistirlo. Para tareas de "
                "generación (diseño, textos, código, creatividad) responde directamente "
                "con el contenido. Para tareas especializadas puedes DELEGAR en ayudantes: "
                "usa gema_list/agent_list para descubrirlos y gema_run/agent_run para delegar "
                "(el LLM de cada ayudante se asigna automáticamente según lo disponible en la PC). "
                "No invoques herramientas innecesarias. Sé conciso."
            )))

        # Inyectar ENTORNO (SO/shell) para que el modelo use comandos NATIVOS.
        # Sin esto, en Windows intenta uname/ls/cat -> exec falla y el agente
        # concluye "no tengo acceso" en vez de responder.
        try:
            import platform as _plat
            _osn = _plat.system()  # Windows | Linux | Darwin
            if _osn == "Windows":
                _shell_hint = ("cmd.exe — usa 'dir', 'type', 'systeminfo', 'ver', 'where'; "
                               "NO 'ls'/'cat'/'uname'/'grep'")
            elif _osn == "Darwin":
                _shell_hint = "sh/zsh — usa 'ls', 'cat', 'uname'"
            else:
                _shell_hint = "sh — usa 'ls', 'cat', 'uname'"
            _env_note = (f"\n\nENTORNO DE EJECUCION: SO={_osn}. Shell={_shell_hint}. "
                         f"Directorio de trabajo: ./workspace. Usa SOLO comandos nativos de este SO. "
                         f"IMPORTANTE: la tool exec corre con cwd=la RAÍZ del proyecto; referí los archivos "
                         f"por su ruta desde la raíz (ej: 'python workspace/mi_script.py') y los archivos que "
                         f"creás con write_file van a ./workspace.")
            if messages and messages[0].role == "system":
                messages[0] = LLMMessage(role="system", content=(messages[0].content or "") + _env_note)
        except Exception:
            pass

        # AGENTS.md jerárquico: root + ancestros de los paths relevantes.
        try:
            from pathlib import Path as _P
            from src.core.agents_md import system_block as _agents_block
            _root = _P(__file__).resolve().parents[2]
            _paths = [str(_root / "workspace")]
            for _w in (task or "").split():
                if "/" in _w or "\\" in _w:
                    _paths.append(str(_root / _w.lstrip("/\\")))
            _blk = _agents_block(_paths, _root)
            if _blk and messages and messages[0].role == "system":
                messages[0] = LLMMessage(role="system", content=(messages[0].content or "") + _blk)
        except Exception:
            pass

        # Ayudantes + gemas disponibles (lo inyecta el server para que el agente delegue).
        if extra_system and messages and messages[0].role == "system":
            try:
                messages[0] = LLMMessage(role="system", content=(messages[0].content or "") + "\n\n" + str(extra_system))
            except Exception:
                pass

        # Plan-mode: si aplica, inyectar plan:policy tras el system prompt.
        # La guía se mantiene hasta fin de batch aunque se apruebe a mitad del
        # run (Approve no la retira: solo habilita la salida al final).
        _plan_active = False
        try:
            if plan_mode is not None and hasattr(plan_mode, "policy_applies"):
                _plan_active = bool(plan_mode.policy_applies(session_id))
            elif plan_mode is not None:
                _plan_active = bool(plan_mode.is_active())
        except Exception:
            _plan_active = False
        if _plan_active:
            try:
                messages.insert(1, LLMMessage(role="system", content=plan_mode.policy_guide()))
            except Exception:
                pass
        # Load history from session_log if available
        history_limit = 20
        if self.session_log is not None:
            try:
                # Ventana de los 20 mensajes MAS RECIENTES: get_messages es ASC,
                # asi que hay que paginar desde el final — con limit asecas se
                # cargaban los 20 mas viejos de la sesion y se perdia el
                # contexto reciente (capa 2, contaminacion/olvido).
                _total = self.session_log.count_messages(session_id)
                _off = max(0, _total - history_limit)
                recent = self.session_log.get_messages(session_id, limit=history_limit, offset=_off)
                # Skip the just-appended user message duplicate
                for m in recent[:-1]:
                    if m.role in ("user", "assistant"):
                        messages.append(LLMMessage(role=m.role, content=m.content))
                    elif m.role == "tool":
                        # session_log NO persiste los tool_calls del assistant,
                        # asi que reenviar el tool tal cual deja mensajes
                        # huerfanos (400 en providers OpenAI-compat, capa 10).
                        # Se reencuadra como nota de asistente: conserva la
                        # informacion sin romper el protocolo.
                        _label = m.tool_name or "herramienta"
                        _narr = (m.content or "")[:2000]
                        messages.append(LLMMessage(role="assistant", content=f"[resultado {_label}] {_narr}"))
                    # role=="system" se ignora: el system prompt fresco ya esta
                    # inyectado arriba. Evita instrucciones conflictivas y
                    # bloquea system-msgs inyectables via POST /sessions/{id}/messages.
            except Exception as e:
                logger.debug(f"load history falló: {e}")

        # Add current task as user message — SIEMPRE al final: aunque exista un
        # duplicado identico en el historial, la tarea actual es lo ultimo que
        # debe ver el modelo (el just-appended se descarto en recent[:-1]).
        if not (messages and messages[-1].role == "user" and messages[-1].content == task):
            messages.append(LLMMessage(role="user", content=task,
                                       images=tuple(images) if images else None))
        elif images and not getattr(messages[-1], "images", None):
            # La tarea ya estaba en el historial: igual hay que adjuntar la visión.
            import dataclasses as _dc
            messages[-1] = _dc.replace(messages[-1], images=tuple(images))

        # Publicar las imagenes del turno en el registry para que la delegacion
        # (gema_run/agent_run) las herede (sin esto el sub-agente no ve la imagen).
        try:
            if self.tools is not None:
                self.tools.current_images = list(images or [])
        except Exception:
            pass

        # Compaction pre-check
        if self.compaction is not None:
            try:
                dict_msgs = self._to_dicts(messages)
                # Use router to decide if we need compaction via token estimate
                max_tokens = self.context_max_tokens
                if hasattr(self.compaction, "maybe_compact") and self.compaction.maybe_compact(dict_msgs, max_tokens):
                    await self._distill(session_id)
                    _limit = int(max_tokens * self.compact_threshold)
                    if getattr(self, "router", None) is not None and hasattr(self.compaction, "summarize_with_llm"):
                        dict_msgs, cmetrics = await self.compaction.summarize_with_llm(
                            dict_msgs, _limit, self.router)
                    else:
                        dict_msgs, cmetrics = await self.compaction.compact(dict_msgs, _limit)
                    messages = self._rebuild_messages(dict_msgs)
            except Exception as e:
                logger.debug(f"compaction pre-check falló: {e}")

        # Prepare tools for LLM
        openai_tools: list[dict] | None = None
        if self.tools is not None:
            try:
                openai_tools = self.tools.to_openai_tools()
                if not openai_tools:
                    openai_tools = None
            except Exception:
                openai_tools = None

        # Main loop
        iteration = 0
        max_iters = self.budget.max_iterations
        retry_budget = getattr(self.budget, "max_iterations", 25)  # fallback
        # Config error policy
        error_retry_limit = 3
        error_replan_limit = 2
        if hasattr(self, "_error_policy"):
            error_retry_limit = self._error_policy.get("retry", 3)
            error_replan_limit = self._error_policy.get("replan", 2)

        while iteration < max_iters:
            if self.budget.exhausted:
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"presupuesto agotado: {self.budget.reason}"))
                final_output = f"[presupuesto agotado: {self.budget.reason}]"
                break

            iteration += 1
            self.budget.tick()
            _turn_t0 = time.monotonic()

            async def _close_turn(reason: str, _t0: float = _turn_t0, _it: int = iteration) -> None:
                _dur = max(0.0, (time.monotonic() - _t0) * 1000.0)
                await self._emit_cycle_async(trajectory, "step/end", _it, duration_ms=_dur, end_reason=reason)
                await self._emit_cycle_async(trajectory, "turn/end", _it, duration_ms=_dur, end_reason=reason)

            # ---- Steer: mensajes inyectados por el usuario durante el run ----
            if callable(steer):
                try:
                    _steer_msgs = steer() or []
                except Exception:
                    _steer_msgs = []
                for _s in _steer_msgs:
                    if _s:
                        messages.append(LLMMessage(role="user", content=f"[steer del usuario] {_s}"))
                        try:
                            await self._emit_cycle_async(trajectory, "steer", iteration, content=str(_s)[:500])
                        except Exception:
                            pass

            # ---- TURN/STEP start ----
            await self._emit_cycle_async(trajectory, "turn/start", iteration, content=f"turno {iteration}")
            await self._emit_cycle_async(trajectory, "step/start", iteration, content=f"paso {iteration}")

            # ---- THINK: LLM call ----
            steps.append(LoopStep(iteration=iteration, phase="think", content=f"pensando (iter {iteration})"))
            llm_resp = None
            try:
                if self.router is None:
                    raise RuntimeError("sin enrutador de proveedores configurado")
                # Hooks pre_llm_call
                if self.hooks is not None and self.hooks.has_handlers("pre_llm_call"):
                    await self.hooks.emit("pre_llm_call", {"iteration": iteration, "messages": len(messages), "hint": hint})
                # Router.chat con fallback (o streaming token-a-token si hay on_delta)
                if getattr(self, "_on_delta", None) is not None and hasattr(self.router, "stream"):
                    llm_resp = await self._stream_turn(messages, openai_tools, hint, force_tools, require_vision=bool(images))
                elif hasattr(self.router, "chat"):
                    llm_resp = await self.router.chat(messages, tools=openai_tools, hint=hint, force_tools=force_tools, model=getattr(self, "_req_model", None), require_vision=bool(images))
                else:
                    # Fallback: resolve then chat
                    routed = self.router.resolve(task_hint=hint)
                    if routed is None:
                        raise RuntimeError(f"sin proveedor para pista={hint}")
                    llm_resp = await routed.provider.chat(messages, tools=openai_tools, model=routed.model)
                # Hooks post_llm_call
                if self.hooks is not None and self.hooks.has_handlers("post_llm_call"):
                    await self.hooks.emit("post_llm_call", {"iteration": iteration, "finish_reason": llm_resp.finish_reason, "has_tool_calls": llm_resp.has_tool_calls})
            except Exception as e:
                err_str = str(e)
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"Error LLM: {err_str}"))
                # Check for context window error -> reactive compact
                # Solo un overflow REAL de contexto dispara reactive-compact (no "rate limit"/429).
                import re as _re_ctx
                _elow = err_str.lower()
                _ctx_overflow = ("rate limit" not in _elow and "quota" not in _elow and
                                 bool(_re_ctx.search(r"context (window|length)|maximum context|too many tokens|prompt is too long|context[_\s]?limit|exceed.{0,20}context|input is too long", _elow)))
                if _ctx_overflow:
                    if self.compaction is not None:
                        try:
                            dict_msgs = self._to_dicts(messages)
                            dict_msgs = await self.compaction.reactive_compact(dict_msgs, err_str)
                            messages = self._rebuild_messages(dict_msgs)
                            await _close_turn("compact-retry")
                            continue  # retry LLM call with compacted context
                        except Exception:
                            pass
                final_output = f"[Error LLM: {err_str}]"
                await _close_turn("error")
                break

            if llm_resp is None:
                final_output = "[sin respuesta LLM]"
                await _close_turn("error")
                break

            # Track tokens. El budget cuenta SOLO los tokens de GENERACIÓN (completion):
            # contar el prompt reenviado cada turno hace que el acumulado llegue a 32k en
            # 2-4 turnos y corte el run prematuramente (bug). total_tokens = observabilidad.
            usage = getattr(llm_resp, "usage", {}) or {}
            _completion = int(usage.get("completion_tokens", 0) or 0)
            if not _completion:
                _completion = len((llm_resp.content or "")) // 4
            turn_tokens = int(usage.get("total_tokens", 0) or _completion)
            total_tokens += turn_tokens
            self.budget.add_tokens(_completion)

            # Append assistant message to history
            assistant_content = llm_resp.content or ""
            # Emitir el RAZONAMIENTO del modelo (si lo hay) via callback -> la UI lo
            # muestra como una linea "Pensando…" colapsable (estilo opencode).
            _rc = (getattr(llm_resp, "reasoning_content", "") or "").strip()
            if _rc and getattr(self, "_on_reasoning", None) is not None:
                try:
                    await self._on_reasoning(_rc)
                except Exception:
                    pass
            # Max-tokens: si el provider cortó por límite y no hay contenido ni
            # tool calls, NO inyectar un assistant vacío (rompe el protocolo y
            # contamina la trayectoria). Se cierran turno/paso y se sale.
            _finish = str(getattr(llm_resp, "finish_reason", "") or "").lower()
            _max_tok_stop = _finish in ("length", "max_tokens", "limit", "token_limit")
            if _max_tok_stop and not assistant_content.strip() and not llm_resp.has_tool_calls:
                steps.append(LoopStep(iteration=iteration, phase="observe", content="corte por max_tokens sin contenido: sin assistant vacío", end_reason="max_tokens"))
                final_output = final_output or "[corte por max_tokens sin contenido]"
                await _close_turn("max_tokens")
                break
            # Antes de declarar \"vacío\", intentar extraer tool_calls de texto
            # (modelos locales ponen <tool_call>JSON</tool_call> en reasoning_content)
            _pre_tcs: list = []
            if not llm_resp.has_tool_calls and not assistant_content.strip():
                _pre_tcs = self._extract_text_tool_calls(getattr(llm_resp, "reasoning_content", "") or "")
                # también probar el fallback <tool_call> que ya extrae LlamaCppProvider arriba,
                # pero por si el provider no lo pobló, usar extracción textual
                if not _pre_tcs:
                    _src = getattr(llm_resp, "reasoning_content", "") or ""
                    if "<tool_call" in _src:
                        import re as _re_pre, json as _json_pre
                        for _m in _re_pre.finditer(r"<tool_call[^>]*>\s*(\{.*?\})\s*</tool_call>", _src, flags=_re_pre.DOTALL):
                            try:
                                _obj = _json_pre.loads(_m.group(1))
                                _name = str(_obj.get("name", _obj.get("tool", ""))).strip()
                                _args = _obj.get("arguments", _obj.get("args", _obj.get("parameters", {})))
                                if isinstance(_args, str):
                                    try: _args = _json_pre.loads(_args) if _args else {}
                                    except Exception: _args = {}
                                if _name and isinstance(_args, dict):
                                    from .llm_provider import ToolCall as _TCPre
                                    _pre_tcs.append(_TCPre(id=f"pre-{len(_pre_tcs)}", name=_name, arguments=_args))
                            except Exception:
                                continue
                if _pre_tcs:
                    # hay tool calls reales — no tratar como vacío, dejar que el flujo normal los despache
                    pass
                else:
                    _reason = (getattr(llm_resp, "reasoning_content", "") or "").strip()
                    _empty_retries = getattr(self, "_empty_retries", 0)
                    if _reason:
                        _reason_retries = getattr(self, "_reason_retries", 0)
                        if _reason_retries >= 3:
                            _fb = self._synthesize_from_tools(messages)
                            final_output = _fb or f"[razonamiento sin respuesta final tras {_reason_retries} intentos]"
                            success = bool(_fb)
                            await _close_turn("reason-only")
                            break
                        self._reason_retries = _reason_retries + 1  # type: ignore[attr-defined]
                        steps.append(LoopStep(iteration=iteration, phase="decide", content="razonamiento sin respuesta final: se continúa"))
                        messages.append(LLMMessage(role="assistant", content=f"[razonamiento interno: {_reason[:2000]}]"))
                        messages.append(LLMMessage(role="user", content="Continúa: usa tus herramientas si la tarea lo requiere, o da tu respuesta final."))
                        await _close_turn("empty-retry")
                        continue
                    elif _empty_retries < 3:
                        self._empty_retries = _empty_retries + 1  # type: ignore[attr-defined]
                        steps.append(LoopStep(iteration=iteration, phase="decide", content=f"respuesta vacía ({self._empty_retries}/3): reintentando"))
                        messages.append(LLMMessage(role="user", content="Tu respuesta anterior llegó vacía. Responde de nuevo, de forma concisa."))
                        await _close_turn("empty-retry")
                        continue
                    else:
                        # Agotados los reintentos: NO declarar éxito con output vacío.
                        # Sintetizar una respuesta desde el último resultado de tool (si lo hay).
                        _fb = self._synthesize_from_tools(messages)
                        final_output = _fb or "[sin respuesta del modelo tras varios intentos]"
                        success = bool(_fb)
                        steps.append(LoopStep(iteration=iteration, phase="decide", content="respuesta vacía agotada: " + ("sintetizada de tools" if _fb else "sin output")))
                        await _close_turn("empty-exhausted")
                        break
            messages.append(LLMMessage(role="assistant", content=assistant_content, tool_calls=llm_resp.tool_calls if llm_resp.has_tool_calls else ()))

            # Trajectory
            _resp_model = getattr(llm_resp, "model", "") or ""
            if llm_resp.has_tool_calls:
                for tc in llm_resp.tool_calls:
                    trajectory.append_assistant(assistant_content, iteration=iteration, tool_name=tc.name, tool_call_id=tc.id, tool_args=tc.arguments, turn=iteration, step="assistant", tokens=turn_tokens, model=_resp_model)
            else:
                trajectory.append_assistant(assistant_content, iteration=iteration, turn=iteration, step="assistant", tokens=turn_tokens, model=_resp_model)

            # Session log (NO persistir assistant vacio: ensucia historial/UI con
            # burbujas vacias — pasa cuando el modelo solo emite tool_calls).
            if self.session_log is not None and (assistant_content or "").strip():
                try:
                    from .session_log import Message
                    self.session_log.append(session_id, Message(role="assistant", content=assistant_content, model=getattr(llm_resp, "model", ""), tokens=turn_tokens))
                except Exception:
                    pass

            # ---- DECIDE: tool_calls vs final ----
            _tcs = list(llm_resp.tool_calls) if llm_resp.has_tool_calls else []
            if not _tcs:
                # Respaldo: modelos pequeños escupen JSON en texto (content o
                # reasoning_content) en vez de tool_calls.
                # {"tool":"echo","arguments":{...}} o {"tool":"echo","text":"..."}
                _tcs = self._extract_text_tool_calls(assistant_content)
                if not _tcs:
                    _tcs = self._extract_text_tool_calls(getattr(llm_resp, "reasoning_content", "") or "")
                if _tcs:
                    steps.append(LoopStep(iteration=iteration, phase="decide", content=f"tool_calls extraídos de texto: {len(_tcs)}"))
                    # Adjuntarlos al mensaje assistant recien agregado: sin esto,
                    # los mensajes tool que siguen quedarian huerfanos (el
                    # provider veria tool sin tool_calls previos) — capa 10.
                    if messages and messages[-1].role == "assistant" and not getattr(messages[-1], "tool_calls", None):
                        messages[-1] = LLMMessage(role="assistant", content=assistant_content, tool_calls=tuple(_tcs))
            if not _tcs:
                # Final answer
                steps.append(LoopStep(iteration=iteration, phase="decide", content=f"respuesta final ({len(assistant_content)} caracteres)"))
                final_output = assistant_content
                success = True
                await _close_turn("stop")
                break
            # ---- ACT: pre-dispatch paralelo de herramientas de SOLO LECTURA ----
            # Las de escritura/exec quedan secuenciales (preservan orden + efectos).
            _pre: dict[int, Any] = {}
            try:
                if len(_tcs) > 1 and _parallel_tools_on() and self.tools is not None:
                    from .concurrency import gather_limited
                    _safe = []
                    for _i, _tc in enumerate(_tcs):
                        _n = (_tc.name or "").lower()
                        if _n not in _READONLY_TOOLS:
                            continue  # fail-closed: las mutantes se saltan y corren serial en orden
                        _a = _tc.arguments
                        if isinstance(_a, str):
                            try:
                                _a = json.loads(_a) if _a else {}
                            except Exception:
                                _a = {}
                        _safe.append((_i, _tc.name or _n, _a if isinstance(_a, dict) else {}))
                    if len(_safe) > 1:
                        _res = await gather_limited([
                            self.tools.dispatch(n, a, scope=self.scope, hooks=self.hooks)
                            for (_i, n, a) in _safe
                        ])
                        for (_i, _n, _a), _r in zip(_safe, _res):
                            _pre[_i] = _r
                        steps.append(LoopStep(iteration=iteration, phase="act", content=f"act: {len(_safe)} herramientas de solo-lectura en paralelo"))
            except Exception:
                _pre = {}
            # ---- ACT: despachar cada llamada de herramienta ----
            # Usa _tcs (reales o extraídos del texto) en vez de llm_resp.tool_calls
            for _idx, tc in enumerate(_tcs):
                tool_name = tc.name
                tool_args = tc.arguments if isinstance(tc.arguments, dict) else {}
                # If arguments is a JSON string (from llama.cpp), parse it
                if isinstance(tc.arguments, str):
                    try:
                        tool_args = json.loads(tc.arguments) if tc.arguments else {}
                    except Exception:
                        tool_args = {}
                steps.append(LoopStep(iteration=iteration, phase="decide", content=f"decide: {tool_name}({json.dumps(tool_args, ensure_ascii=False)[:300]})", tool_used=tool_name))

                # Act
                tool_result_str = ""
                tool_success = False
                # Anti doom-loop (deepseek repeat-tool-reminder): cuenta TODAS las llamadas
                # (incl. fallidas). A las 3 y 5 inyecta un RECORDATORIO (no corta: el modelo
                # puede corregir); a las 7 corta de verdad.
                try:
                    _fp = (tool_name, json.dumps(tool_args if isinstance(tool_args, dict) else {}, sort_keys=True, default=str))
                    _cnt = getattr(self, "_tool_repeat", None)
                    if _cnt is None:
                        _cnt = {}
                        self._tool_repeat = _cnt  # type: ignore[attr-defined]
                    _n = _cnt.get(_fp, 0) + 1
                    _cnt[_fp] = _n
                    if _n in (2, 3):
                        try:
                            messages.append(LLMMessage(role="user", content=(
                                f"[recordatorio del sistema] repetiste la herramienta '{tool_name}' {_n} veces con "
                                f"los MISMOS argumentos y no avanzás. CAMBIÁ de estrategia (otros args, otra tool) "
                                f"o da tu respuesta final con lo que ya tenés.")))
                        except Exception:
                            pass
                    elif _n >= 4:
                        final_output = f"[doom-loop: {tool_name} repetido {_n} veces con mismos args — bucle detenido; sin respuesta final]"
                        success = False
                        iteration = max_iters  # force exit
                        break
                except Exception:
                    pass
                try:
                    if self.tools is None:
                        raise KeyError(f"sin registro de herramientas; no se puede despachar {tool_name}")
                    # Dispatch with scope+hooks (usa pre-dispatch paralelo si aplica)
                    if _idx in _pre:
                        _pr = _pre[_idx]
                        if isinstance(_pr, BaseException):
                            raise _pr
                        raw_result = _pr
                    else:
                        raw_result = await self.tools.dispatch(tool_name, tool_args, scope=self.scope, hooks=self.hooks)
                    # Normalize result to string
                    if isinstance(raw_result, str):
                        tool_result_str = raw_result
                    elif isinstance(raw_result, dict):
                        tool_result_str = json.dumps(raw_result, ensure_ascii=False, indent=2)
                    elif raw_result is None:
                        tool_result_str = ""
                    else:
                        tool_result_str = str(raw_result)
                    tool_success = True
                    self.budget.add_tool_calls(1)
                    # Diagnostics-in-the-loop: tras editar/escribir, correr un chequeo
                    # rapido y anexarlo al resultado (el modelo se autocorrige).
                    try:
                        if tool_name in ("write_file", "edit") and isinstance(tool_args, dict) and tool_args.get("path"):
                            from .diagnostics import diagnostics_for, config_enabled
                            if config_enabled(getattr(self, "_cfg", None) or {}):
                                _diag = diagnostics_for(tool_args.get("path"))
                                if _diag:
                                    tool_result_str = (tool_result_str or "") + _diag
                    except Exception:
                        pass
                except Exception as e:
                    err_str = str(e)
                    tool_result_str = self._error_hint(tool_name, e)
                    tool_success = False
                    # Classify error
                    action = self._classify_error(tool_name, err_str)
                    steps.append(LoopStep(iteration=iteration, phase="act", content=f"herramienta {tool_name} falló: {err_str} -> {action.value}", tool_used=tool_name, result=ToolResult(success=False, output="", error=err_str)))
                    if action == ErrorAction.ABORT:
                        final_output = f"[abortado: {tool_name} -> {err_str}]"
                        success = False
                        # Break outer loop
                        iteration = max_iters  # force exit
                        break
                    elif action == ErrorAction.RETRY:
                        cnt = self._retry_counts.get(tool_name, 0)
                        if cnt < error_retry_limit:
                            self._retry_counts[tool_name] = cnt + 1
                            # Se reintentará devolviendo el error al LLM como resultado de herramienta and continuing
                        else:
                            tool_result_str += f" [presupuesto reintentos agotado {cnt}]"
                    elif action == ErrorAction.REPLAN:
                        cnt = self._replan_counts.get(tool_name, 0)
                        if cnt < error_replan_limit:
                            self._replan_counts[tool_name] = cnt + 1
                        else:
                            tool_result_str += f" [presupuesto replanificación agotado]"
                    elif action == ErrorAction.SKIP:
                        tool_result_str = f"[omitido {tool_name}: {err_str}]"
                        tool_success = True  # treat as non-fatal

                # Un éxito limpia el presupuesto de esa tool; los fallos
                # consecutivos (aunque cambien args) se escalan en vez de
                # dejar al modelo reintentando indefinidamente.
                if tool_success:
                    self._retry_counts.pop(tool_name, None)
                    self._replan_counts.pop(tool_name, None)
                    self._consecutive_failures = 0  # type: ignore[attr-defined]
                elif not str(tool_result_str).startswith("[omitido"):
                    _cf = int(getattr(self, "_consecutive_failures", 0)) + 1
                    self._consecutive_failures = _cf  # type: ignore[attr-defined]
                    if _cf >= 6:
                        final_output = (
                            f"[abortado: {_cf} fallos consecutivos de herramientas; "
                            f"último: {tool_name} -> {tool_result_str[:300]}]")
                        success = False
                        iteration = max_iters
                        break
                    if _cf == 3:
                        messages.append(LLMMessage(role="user", content=(
                            "[recordatorio del sistema] 3 fallos consecutivos de herramientas. "
                            "No reintentes lo mismo: leé el error, inspeccioná el archivo o el "
                            "entorno, y cambiá de enfoque antes de la próxima llamada.")))

                # Recorte con cabeza+cola + spill: el final suele traer el error.
                if len(tool_result_str) > 8000:
                    try:
                        from .spill import clip_output
                        tool_result_str = clip_output(
                            tool_result_str,
                            session_id=session_id,
                            tool=tool_name,
                            call_id=getattr(tc, "id", "") or "",
                        )
                    except Exception:
                        tool_result_str = tool_result_str[:8000] + f"\n[...truncado {len(tool_result_str)-8000} caracteres]"

                # Observar: agregar resultado de herramienta to messages + session + trajectory
                steps.append(LoopStep(iteration=iteration, phase="act", content=f"act: {tool_name}", tool_used=tool_name, result=ToolResult(success=tool_success, output=tool_result_str[:2000])))
                trajectory.append_tool_result(tool_name, tool_result_str, success=tool_success, iteration=iteration, tool_call_id=tc.id, turn=iteration, step="tool", tokens=0, model=_resp_model)
                messages.append(LLMMessage(role="tool", content=tool_result_str, tool_call_id=tc.id))
                if self.session_log is not None:
                    try:
                        from .session_log import Message
                        self.session_log.append(session_id, Message(role="tool", content=tool_result_str, tool_name=tool_name, tool_call_id=tc.id))
                    except Exception:
                        pass
                if self.event_store is not None:
                    try:
                        self.event_store.append_event("tool_result" if tool_success else "error", {"tool": tool_name, "result": tool_result_str[:2000], "success": tool_success}, session_id=session_id)
                    except Exception:
                        pass

            # ---- OBSERVE: budget check + compaction ----
            if self.budget.exhausted:
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"presupuesto agotado tras iter {iteration}: {self.budget.reason}"))
                final_output = final_output or "[presupuesto agotado antes de respuesta final]"
                await _close_turn("budget")
                break

            # Maybe compact if approaching limit
            if self.compaction is not None and len(messages) > 10:
                try:
                    dict_msgs = self._to_dicts(messages)
                    max_tokens = self.context_max_tokens
                    if self.compaction.maybe_compact(dict_msgs, max_tokens):
                        await self._distill(session_id)
                        # Solo compactar system+historial, mantener resultados de herramientas recientes
                        _limit = int(max_tokens * self.compact_threshold)
                        if getattr(self, "router", None) is not None and hasattr(self.compaction, "summarize_with_llm"):
                            dict_msgs, _ = await self.compaction.summarize_with_llm(
                                dict_msgs, _limit, self.router)
                        else:
                            dict_msgs, _ = await self.compaction.compact(dict_msgs, _limit)
                        messages = self._rebuild_messages(dict_msgs)
                        steps.append(LoopStep(iteration=iteration, phase="observe", content=f"contexto compactado ({len(dict_msgs)} mensajes)"))
                except Exception as e:
                    logger.debug(f"observe compaction falló: {e}")
            # Fin de turno con herramientas: se cierra y el while abre el siguiente.
            await _close_turn("tool_calls")

        # Finalize
        duration_ms = (time.monotonic() - t0) * 1000
        if not final_output and trajectory.steps:
            # Fallback: last assistant content
            last = trajectory.last_assistant
            if last:
                final_output = last.content
                success = bool(final_output and "error" not in final_output.lower()[:100])

        if self.event_store is not None:
            try:
                self.event_store.append_event("assistant_message" if success else "error", {"content": final_output[:2000], "success": success, "iterations": iteration}, session_id=session_id)
            except Exception:
                pass
        if self.hooks is not None:
            try:
                await self.hooks.emit("session_end", {"session_id": session_id, "success": success, "iterations": iteration, "total_tokens": total_tokens})
            except Exception:
                pass
        # Session summary update
        if self.session_log is not None and final_output and success and not final_output.lstrip().startswith("["):
            try:
                # Don't overwrite existing summary if already set via compact
                sess = self.session_log.get_session(session_id)
                if sess is not None and not sess.summary:
                    self.session_log.update_session(session_id, summary=final_output.strip()[:200])
            except Exception:
                pass

        return LoopResult(
            task=task,
            success=success,
            final_output=final_output,
            steps=steps,
            iterations=iteration,
            total_tokens=total_tokens,
            duration_ms=duration_ms,
            trajectory=trajectory,
        )

    def _classify_error(self, tool: str, err: str) -> ErrorAction:
        try:
            return classify_error(tool, err)
        except Exception:
            return ErrorAction.REPLAN

    @staticmethod
    def _error_hint(tool_name: str, e: BaseException) -> str:
        """Error + acción sugerida: decir qué hacer, no solo qué falló."""
        msg = str(e)
        low = msg.lower()
        if isinstance(e, PermissionError):
            hint = ("la herramienta fue bloqueada por permisos/scope: verificá que la ruta esté "
                    "dentro de las raíces permitidas, que el modo no sea plan (solo lectura) o "
                    "pedí confirmación explícita.")
        elif isinstance(e, TimeoutError) or "timeout" in low or "timed out" in low:
            hint = ("el comando excedió el tiempo: reducí el alcance (un archivo, un test) o "
                    "usá una tarea en background y consultá su salida.")
        elif isinstance(e, FileNotFoundError):
            hint = "la ruta no existe: confirmá con list_dir/glob antes de reintentar."
        elif isinstance(e, (ValueError, TypeError)):
            hint = f"revisá los argumentos contra el esquema de {tool_name}."
        else:
            hint = "leé el error completo; si se repite, cambiá de estrategia en vez de reintentar igual."
        return f"Error: {msg}\n[acción sugerida] {hint}"

    def exit_plan_mode(self, plan_mode, approved: bool = False, request_id: str | None = None) -> bool:
        """Delegación fail-closed: fuera de plan (u sin controller) siempre falla."""
        try:
            if plan_mode is None:
                return False
            return bool(plan_mode.exit_plan_mode(approved=approved, request_id=request_id))
        except Exception:
            return False

    def set_error_policy(self, retry: int = 3, replan: int = 2) -> None:
        self._error_policy = {"retry": retry, "replan": replan}

    def reset_budget(self, max_iterations: int | None = None, max_tokens: int | None = None) -> None:
        if max_iterations is not None:
            self.budget.max_iterations = max_iterations
        if max_tokens is not None:
            self.budget.max_tokens = max_tokens
        self.budget.reset()
        self._retry_counts.clear()
        self._replan_counts.clear()
        self._consecutive_failures = 0  # type: ignore[attr-defined]
