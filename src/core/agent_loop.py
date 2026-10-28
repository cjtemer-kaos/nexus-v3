"""Bucle Agente — TDAO (Think-Decide-Act-Observe) + budget + error classifier + heartbeat.

Alcance: presupuesto de iteraciones, clasificación de errores y heartbeat entre tool calls.
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
import hashlib
import json
import re
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
    "code_search", "repo_map", "recall", "memory_recall", "brain_recall", "search_observations",
    "web_fetch", "web_search", "fetch", "mcp_list", "gema_list", "agent_list",
    "get_system_info", "system_resources", "status", "graph", "knowledge_graph",
    "fts_search", "find_related", "get_entities", "latest_topic", "find_path",
}


def _parallel_tools_on() -> bool:
    return _os.environ.get("NEXUS_PARALLEL_TOOLS", "1").lower() not in ("0", "false", "no")


def _wire_tool_calls(tcs) -> tuple[dict, ...]:
    """Llamadas -> shape del wire (el mismo que `_to_dicts` y que se guarda en la DB)."""
    out: list[dict] = []
    for tc in tcs or ():
        out.append({
            "id": getattr(tc, "id", "") or "",
            "type": "function",
            "function": {
                "name": getattr(tc, "name", "") or "",
                "arguments": getattr(tc, "arguments", {}) or {},
            },
        })
    return tuple(out)


def _tool_calls_from_rows(raw) -> tuple[ToolCall, ...]:
    """Columna `tool_calls` (lista de dicts) -> ToolCall. Acepta el shape plano también."""
    out: list[ToolCall] = []
    for tc in raw or ():
        if isinstance(tc, ToolCall):
            out.append(tc)
            continue
        if not isinstance(tc, dict):
            continue
        _fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        out.append(ToolCall(
            id=str(tc.get("id") or ""),
            name=str((_fn.get("name") if _fn else "") or tc.get("name") or ""),
            arguments=(_fn.get("arguments") if _fn else None) or tc.get("arguments") or {},
        ))
    return tuple(out)


_SIN_RESULTADO = "[sin resultado: el turno se interrumpió antes de ejecutar esta herramienta]"


def _replay_history(rows: list[dict]) -> list[LLMMessage]:
    """Historial persistido -> mensajes para el provider, con los pares intactos.

    `assistant(tool_calls)` seguido de sus `tool` es lo que el protocolo exige.
    Dos casos se reparan acá:

    - filas viejas sin `tool_calls` (antes de persistirlas): el `tool` queda
      huérfano y se degrada a nota de assistant, como se hacía siempre;
    - cadena cortada (proceso muerto, ventana de historial que arranca en el
      medio): se cierra con un resultado sintético, porque un `assistant` con
      `tool_calls` sin todas sus respuestas es un 400 en providers OpenAI-compat.
    """
    out: list[LLMMessage] = []
    pendientes: list[str] = []

    def cerrar_pendientes() -> None:
        for _id in pendientes:
            out.append(LLMMessage(role="tool", content=_SIN_RESULTADO, tool_call_id=_id))
        pendientes.clear()

    for m in rows:
        rol = m.get("role")
        if rol == "tool":
            _id = str(m.get("tool_call_id") or "")
            if _id and _id in pendientes:
                pendientes.remove(_id)
                out.append(LLMMessage(role="tool", content=m.get("content") or "",
                                      tool_call_id=_id))
            else:
                # Sin llamada que lo empareje: nota de assistant (comportamiento
                # histórico). No se manda como rol tool para no romper el wire.
                _label = m.get("tool_name") or "herramienta"
                _narr = (m.get("content") or "")[:2000]
                out.append(LLMMessage(role="assistant", content=f"[resultado {_label}] {_narr}"))
            continue
        cerrar_pendientes()
        if rol in ("user", "assistant"):
            _tcs = _tool_calls_from_rows(m.get("tool_calls"))
            out.append(LLMMessage(role=rol, content=m.get("content") or "",
                                  tool_calls=_tcs))
            if _tcs:
                pendientes.extend(tc.id for tc in _tcs)
        # role=="system" se ignora: el system prompt fresco ya va arriba.
    cerrar_pendientes()
    return out


def _tools_from_pin(current: list[dict] | None, pin: dict) -> list[dict] | None:
    """Reconstruye el set de tools del turno pineado.

    Se manda el schema que se envió entonces, en el mismo orden, para que el
    modelo vea la misma lista de capacidades. Se prefiere el schema pineado; si
    un pin viejo no lo guardó, se usa el actual con ese nombre. Un tool que ya
    no existe en el registry se omite: mandarle un schema sin handler haría que
    el modelo llame algo que no se puede ejecutar.
    """
    nombres = [str(n) for n in (pin.get("names") or []) if str(n).strip()]
    if not nombres:
        return None
    pineados: dict[str, dict] = {}
    for schema in (pin.get("schemas") or []):
        nombre = str(((schema or {}).get("function") or {}).get("name") or "")
        if nombre:
            pineados[nombre] = schema
    actuales: dict[str, dict] = {}
    for schema in (current or []):
        nombre = str(((schema or {}).get("function") or {}).get("name") or "")
        if nombre:
            actuales[nombre] = schema
    out: list[dict] = []
    for nombre in nombres:
        schema = pineados.get(nombre) or actuales.get(nombre)
        if schema:
            out.append(schema)
    return out or None


# ---------------------------------------------------------------------------
# Calibracion del estimador de tokens frente al `prompt_tokens` real.
# El estimador `len/4` subcuenta en CJK, minificado y JSON denso. Cuando el
# provider reporta `prompt_tokens` para el request que acabamos de mandar,
# guardamos el factor real `actual/estimado` por sesion. El proximo turno usa
# un presupuesto de contexto dividido por ese factor (cap 4x). Nunca afloja:
# factor < 1 se ignora. Module-level porque AgentLoop se instancia por request.
# ---------------------------------------------------------------------------
_CALIBRATION: dict[str, float] = {}
_CALIBRATION_MAX = 512
UNDERESTIMATE_CAP = 4.0


def record_usage_calibration(session_id: str, estimated: int, actual: int) -> None:
    """Registra el factor de subestimacion del request recien enviado."""
    try:
        if not session_id or estimated <= 0 or actual <= 0:
            return
        factor = min(UNDERESTIMATE_CAP, max(1.0, float(actual) / float(estimated)))
        if len(_CALIBRATION) >= _CALIBRATION_MAX and session_id not in _CALIBRATION:
            _CALIBRATION.pop(next(iter(_CALIBRATION)), None)
        _CALIBRATION[session_id] = factor
    except Exception:
        return


def get_underestimate_factor(session_id: str) -> float:
    return _CALIBRATION.get(session_id, 1.0)

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


def turno_hizo_trabajo(result) -> bool:
    """True si el turno uso herramientas y no se declaro terminado.

    Lo usa la continuacion por rondas del servidor: un turno que solo respondio
    (sin fase act) es la senal de que el modelo cerro, y `LISTO:` es la senal
    explicita que se le pide en la continuacion. Una tarea grande nunca entra
    en un solo turno y el modelo tiende a cerrar con "que falta" en vez de
    seguir trabajando; esto permite que el harness siga solo.
    """
    try:
        if "LISTO:" in (getattr(result, "final_output", "") or "").upper():
            return False
        return any((getattr(s, "phase", "") == "act") and getattr(s, "tool_used", "")
                   for s in getattr(result, "steps", []) or [])
    except Exception:
        return False


@dataclass
class LoopResult:
    task: str
    success: bool
    final_output: str
    steps: list[LoopStep]
    iterations: int
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: float = 0
    trajectory: Trajectory | None = None
    compact_metrics: Any | None = None
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "success": self.success,
            "final_output": self.final_output,
            "iterations": self.iterations,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": round(self.duration_ms, 1),
            "model": self.model,
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
        approval=None,
        config: dict | None = None,
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
        # Gate HITL: lo arma el servidor (interactivo) o el subagente (never).
        self.approval = approval
        # Presupuesto de contexto y umbral de compactación (config.session.*).
        # Antes estaban fijos en 32000/0.75 y la config no tenía efecto.
        self.context_max_tokens = int(context_max_tokens or 32000)
        self.compact_threshold = float(compact_threshold or 0.75)
        self._cfg = config or {}

        # Umbrales anti-loop configurables (defaults conservadores para tests
        # que instancian AgentLoop sin config; produccion pasa config.yaml).
        _anti = ((self._cfg.get("agent_loop") or {}).get("anti_loop") or {})
        try:
            self.anti_loop_repeat_warn = tuple(
                int(x) for x in (_anti.get("repeat_warn") or [2, 3]))
        except Exception:
            self.anti_loop_repeat_warn = (2, 3)
        self.anti_loop_repeat_stop = int(_anti.get("repeat_stop", 4) or 4)
        self.anti_loop_failures_warn = int(_anti.get("failures_warn", 3) or 3)
        self.anti_loop_failures_stop = int(_anti.get("failures_stop", 6) or 6)
        # Por herramienta (aunque cambien los args): avisar y cortar.
        try:
            self.anti_loop_tool_warn = int(_anti.get("tool_warn", 5) or 5)
            self.anti_loop_tool_stop = int(_anti.get("tool_stop", 10) or 10)
        except Exception:
            self.anti_loop_tool_warn, self.anti_loop_tool_stop = 5, 10

        # Per-task retry/replan counters
        self._retry_counts: dict[str, int] = {}
        self._replan_counts: dict[str, int] = {}
        # Histéresis de compactación: tras compactar no se vuelve a compactar hasta
        # que el contexto crezca de nuevo por encima de este piso (tokens estimados).
        # Sin esto, con resultados de tools grandes el loop compactaba en casi todas
        # las iteraciones (y cada compactación disparaba destilado + LLM de resumen).
        self._no_recompact_below: dict[str, int] = {}

    def _count_tools_tokens(self, openai_tools) -> int:
        """Tokens de los JSON Schema de las tools (viajan en cada request)."""
        try:
            if not openai_tools:
                return 0
            import json as _json
            return len(_json.dumps(openai_tools, ensure_ascii=False)) // 4
        except Exception:
            return 0

    async def _prior_knowledge_block(self, task: str) -> str:
        """Lo que el grafo ya sabe de este pedido, listo para el system prompt.

        Nexus es autonomo si arranca sabiendo. Se consulta la memoria siempre (no
        solo cuando el usuario dice "grafo"): si hay nodos, se citan con su id para
        que el modelo use esos valores; si no hay, se le pide investigar y guardar,
        porque aprender es parte de la tarea, no un extra.
        """
        texto = " ".join((task or "").split())
        if len(texto) < 12:
            return ""
        from .knowledge_memory import fts_search_tolerant, get_knowledge_memory
        km = get_knowledge_memory()
        # exclude_sessions: los episodios que el harness crea por sesion repiten el
        # pedido entero y ganaban siempre, tapando el nodo con la respuesta.
        hits = await asyncio.to_thread(
            fts_search_tolerant, km, texto[:400], 6, exclude_sessions=True)
        filas: list[str] = []
        for h in (hits or [])[:3]:
            titulo = " ".join(str(h.get("title") or "").split())[:120]
            snip = " ".join(str(h.get("snippet") or "").replace("[", "").replace("]", "")
                            .replace("«", "").replace("»", "").split())[:320]
            if not titulo and not snip:
                continue
            filas.append(f"- {titulo} (nodo {h.get('id')}): {snip}")
        if filas:
            return ("\n\nCONOCIMIENTO PREVIO DEL GRAFO (recuperado automaticamente para este "
                    "pedido; si un dato contradice lo que ves ahora, comprobalo y corregi el "
                    "nodo):\n" + "\n".join(filas)
                    + "\nSi al resolver aparecen datos nuevos o corregidos, guardalos con learn.")
        return ("\n\nCONOCIMIENTO PREVIO DEL GRAFO: no hay entradas para este pedido. Si la tarea depende "
                "de como funciona una herramienta, un protocolo o una API que no esta en el "
                "grafo, buscala en internet (web_search/web_fetch), verifica el dato y guardalo "
                "con learn (una entrada por tema, con la fuente) antes de cerrar el turno.")

    async def _resolve_model_context_window(self, hint: str | None) -> None:
        """Anota la ventana de contexto real del modelo que va a atender el turno.

        Se pregunta al proveedor (Ollama `/api/show`, llama.cpp `/props`) porque
        cada modelo tiene la suya y el Modelfile puede haber cambiado. Nunca rompe
        el turno: si no hay dato queda 0 y se usa el presupuesto de config.
        """
        try:
            router = getattr(self, "router", None)
            if router is None or not hasattr(router, "context_length"):
                return
            model = getattr(self, "_req_model", None)
            if not model:
                routed = router.resolve(task_hint=hint)
                model = getattr(routed, "model", "") if routed is not None else ""
            if not model:
                return
            from src.core import context_limits

            native = await router.context_length(model)
            window = context_limits.usable_window(
                model, provider="ollama", cfg=getattr(self, "_cfg", None) or {}, native=native,
            )
            if window and window > 0:
                self._model_ctx_tokens = int(window)
                logger.info(
                    "context: modelo=%s nativo=%s ventana=%d (presupuesto del loop=%d)",
                    model, native or "?", self._model_ctx_tokens, self.context_max_tokens,
                )
        except Exception as e:
            logger.debug("context: no se pudo resolver la ventana del modelo (%s)", e)

    def _effective_context_max_tokens(self, session_id: str) -> int:
        """Presupuesto de contexto corregido por subestimacion del tokenizer.

        Cline escala TODO el presupuesto (trigger, target y costos), no solo el
        trigger: si solo bajara el trigger, la compactacion arrancaria antes pero
        retendria demasiado y volveria a overflowear. Piso 1024 para que un
        factor grande no deje el contexto inutilizable.
        """
        base = self.context_max_tokens
        model_window = int(getattr(self, "_model_ctx_tokens", 0) or 0)
        if model_window > 0:
            # El modelo manda: pedirle mas ventana de la que tiene es un 400 de Ollama.
            base = min(base, model_window)
        factor = get_underestimate_factor(session_id)
        if factor <= 1.0:
            return base
        return max(1024, int(base / factor))

    def _should_compact(self, session_id: str, dict_msgs: list[dict], max_tokens: int,
                        est_tokens: int | None = None) -> bool:
        """Compactar sólo si se pasó el umbral Y se superó el piso de histéresis."""
        if self.compaction is None or not hasattr(self.compaction, "maybe_compact"):
            return False
        if est_tokens is None:
            try:
                est_tokens = int(self.compaction.estimate_tokens_for_messages(dict_msgs))
            except Exception:
                est_tokens = 0
        # Los schemas de tools ocupan contexto igual que los mensajes: sin sumarlos
        # el disparo de compactacion llega tarde y el request real desborda.
        tools_n = int(getattr(self, "_tools_tokens_n", 0) or 0)
        est_tokens += tools_n
        floor = int(self._no_recompact_below.get(session_id, 0) or 0)
        if floor and est_tokens <= floor:
            return False
        try:
            self._last_est_tokens = est_tokens  # type: ignore[attr-defined]
        except Exception:
            pass
        if tools_n > 0:
            # `maybe_compact` estima SOLO los mensajes, asi que se le pasa un tope
            # equivalente que ya descuenta los schemas: mismo umbral, cuenta real.
            _thr = max(0.05, float(self.compact_threshold or 0.75))
            max_tokens = max(1, int(round(max_tokens - tools_n / _thr)))
        return bool(self.compaction.maybe_compact(dict_msgs, max_tokens, self.compact_threshold))

    def _note_compaction(self, session_id: str, metrics: Any, dict_msgs: list[dict]) -> None:
        """Registra el piso de histéresis y deja traza de la compactación en el log."""
        try:
            md = metrics.to_dict() if hasattr(metrics, "to_dict") else dict(metrics or {})
        except Exception:
            md = {}
        before = int(md.get("tokens_before", 0) or 0)
        if before <= 0:
            before = int(getattr(self, "_last_est_tokens", 0) or 0)
        after = int(md.get("tokens_after", md.get("compressed_tokens", 0)) or 0)
        if after <= 0:
            try:
                after = int(self.compaction.estimate_tokens_for_messages(dict_msgs))
            except Exception:
                after = 0
        # Tras compactar, dejar aire: no re-compactar hasta crecer 20% sobre lo que quedó.
        self._no_recompact_below[session_id] = int(after * 1.2) if after > 0 else 0
        logger.info(
            "compact sesión %s: %d -> %d tokens est. (piso %d, budget %d, umbral %.2f, capas %s)",
            session_id, before, after, self._no_recompact_below.get(session_id, 0),
            self.context_max_tokens, self.compact_threshold, md.get("layers", []),
        )

    def _persist_compaction_summary(self, session_id: str, dict_msgs: list[dict]) -> None:
        """Guarda el resumen L4 como mensaje real (durable entre reinicios)."""
        if self.session_log is None:
            return
        for m in dict_msgs or []:
            if str(m.get("role") or "") != "system":
                continue
            content = str(m.get("content") or "")
            if not content.startswith("[resumen de "):
                continue
            try:
                prev = self.session_log.get_summary_message(session_id)
                if prev and str(prev.get("content") or "").strip() == content.strip():
                    return
                self.session_log.compact_mark(session_id, content)
                logger.info("resumen durable persistido en sesion %s", session_id)
            except Exception as e:
                logger.debug("no se pudo persistir el resumen: %s", e)
            return

    def _persist_compaction_state(self, session_id: str, metrics: Any,
                                  dict_msgs: list[dict]) -> None:
        """Guarda metricas de compactacion (tokens, capas, resumen) en la DB."""
        if self.session_log is None or not hasattr(self.session_log, "save_compaction_state"):
            return
        try:
            md = metrics.to_dict() if hasattr(metrics, "to_dict") else {}
            prev = None
            try:
                prev = self.session_log.get_summary_message(session_id)
            except Exception:
                prev = None
            layers = md.get("layers_applied") or []
            self.session_log.save_compaction_state(
                session_id,
                tokens_before=int(md.get("original_tokens", 0) or 0),
                tokens_after=int(md.get("compressed_tokens", 0) or 0),
                saved=int(md.get("tokens_saved", 0) or 0),
                layers=list(layers) if isinstance(layers, list) else [],
                actions=len(layers) if isinstance(layers, list) else 0,
                summary=self._compaction_summary(dict_msgs),
                summary_message_id=int(prev["id"]) if prev else None,
            )
        except Exception as e:
            logger.debug("no se pudo persistir el estado de compactacion: %s", e)

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

    def _summary_state(self, session_id: str) -> tuple[str, int]:
        """Resumen durable previo y mensajes nuevos desde el (para no re-resumir)."""
        prev = ""
        progress = 0
        if self.session_log is None:
            return prev, progress
        try:
            sm = self.session_log.get_summary_message(session_id)
            if sm:
                prev = str(sm.get("content") or "").strip()
                after = self.session_log.get_messages_from(
                    session_id, int(sm["id"]), limit=10000)
                progress = len(after or [])
            elif hasattr(self.session_log, "get_compaction_state"):
                st = self.session_log.get_compaction_state(session_id)
                if st and str(st.get("summary") or "").strip():
                    prev = str(st["summary"]).strip()
                    progress = len(self.session_log.get_messages(session_id, limit=10000) or [])
        except Exception:
            pass
        return prev, progress

    async def _emit_compact_hook(self, session_id: str, metrics: Any,
                                 summary: str = "") -> None:
        """Notifica al hook on_compact sin romper el loop si falla."""
        if self.hooks is None or not hasattr(self.hooks, "has_handlers"):
            return
        try:
            if not self.hooks.has_handlers("on_compact"):
                return
            payload = {
                "session_id": session_id,
                "summary": (summary or "")[:2000],
                "metrics": metrics.to_dict() if hasattr(metrics, "to_dict") else {},
            }
            await self.hooks.emit("on_compact", payload)
        except Exception as e:
            logger.debug("hook on_compact falló: %s", e)

    def _compaction_summary(self, dict_msgs: list[dict]) -> str:
        for m in dict_msgs or []:
            if str(m.get("role") or "") != "system":
                continue
            c = str(m.get("content") or "")
            if c.startswith("[resumen de "):
                return c
        return ""

    def _prune_payload(self, messages: list[LLMMessage]) -> list[LLMMessage]:
        """Poda solo el payload que va al LLM; el historial real no se toca."""
        if self.compaction is None or not hasattr(self.compaction, "prune_for_llm"):
            return messages
        try:
            return self.compaction.prune_for_llm(messages)  # type: ignore[arg-type]
        except Exception as e:
            logger.debug("prune_for_llm falló: %s", e)
            return messages

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
        # 2) Escáner de invocaciones en XML, la otra forma en que los modelos
        # escriben la llamada como texto (visto con deepseek-v4.1-flash tras una
        # compactación): <invoke name="repl"><parameter name="code">…</parameter>
        # </invoke>. Sin esto el turno cerraba con el XML crudo como respuesta
        # final y el trabajo quedaba a medias, sin error visible.
        for _bm in _re2.finditer(
                r"(?is)<(?:antml:)?invoke\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</(?:antml:)?invoke>",
                content):
            _nm = _bm.group(1).strip()
            if names and _nm not in names:
                _lc = {n.lower(): n for n in names}
                if _nm.lower() not in _lc:
                    continue
                _nm = _lc[_nm.lower()]
            _body = _bm.group(2)
            _args: dict = {}
            for _pm in _re2.finditer(
                    r"(?is)<(?:antml:)?parameter\s+name\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</(?:antml:)?parameter>",
                    _body):
                _val = _pm.group(2)
                for _a, _b in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                               ("&apos;", "'"), ("&amp;", "&")):
                    _val = _val.replace(_a, _b)
                _args[_pm.group(1).strip()] = _val
            if not _args:
                # Variante corta: <invoke name="repl"><code>…</code></invoke>
                for _km in _re2.finditer(r"(?is)<([a-zA-Z_]\w*)\s*>(.*?)</\1>", _body):
                    _args[_km.group(1)] = _km.group(2)
            if not _args:
                continue
            try:
                out.append(ToolCall(id=f"text-{len(out)}", name=_nm, arguments=_args))
            except Exception:
                continue
            if len(out) >= 5:
                break
        return out

    def _sanear_historial(self, messages: list) -> list:
        """Deja el historial en la forma que el wire OpenAI acepta.

        Cada `assistant` con tool_calls necesita respuesta para TODOS sus ids
        antes del mensaje siguiente, y un `tool` sin llamada previa no se puede
        mandar. Los cortes del loop (guards, presupuesto) y la compactación
        pueden dejar el par incompleto en medio del turno; con la sesión ya
        grande el proveedor devolvía 400 «insufficient tool messages following
        tool_calls» y mataba la corrida entera (pasó en la sesión de eMule, 630
        mensajes). Acá se cierra cada hueco con el resultado sintético y se
        degrada el `tool` huérfano a nota de assistant.
        """
        pend: list[str] = []
        out: list = []
        repair = 0

        def cerrar() -> None:
            nonlocal repair
            for _id in pend:
                out.append(LLMMessage(role="tool", content=_SIN_RESULTADO, tool_call_id=_id))
                repair += 1
            pend.clear()

        for m in messages or []:
            rol = str(getattr(m, "role", "") or "")
            if rol == "tool":
                _id = str(getattr(m, "tool_call_id", "") or "")
                if _id and _id in pend:
                    pend.remove(_id)
                    out.append(m)
                else:
                    repair += 1
                    _c = (getattr(m, "content", "") or "")[:2000]
                    out.append(LLMMessage(
                        role="assistant",
                        content=f"[resultado de herramienta sin llamada previa] {_c}"))
                continue
            cerrar()
            out.append(m)
            if rol == "assistant":
                for tc in (getattr(m, "tool_calls", None) or ()):
                    pend.append(str(getattr(tc, "id", "") or ""))
        cerrar()
        if repair:
            try:
                logger.warning("historial saneado: %d huecos de tool_call reparados", repair)
            except Exception:
                pass
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
                _t_h = time.monotonic()
                res = self.hooks.emit(kind, {"iteration": iteration, "turn": iteration, "session_id": trajectory.session_id})
                if asyncio.iscoroutine(res):
                    await res
                self._perf_hooks_ms = getattr(self, "_perf_hooks_ms", 0.0) + (time.monotonic() - _t_h) * 1000.0
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

    async def _final_wrapup(self, messages, hint, steps, iteration) -> str:
        """Pide un cierre en TEXTO (sin herramientas) cuando el turno se corta.

        Si se agota el presupuesto iterando, o el anti-bucle corta una repeticion,
        el modelo estaba a mitad de la tarea: antes el turno terminaba con
        `[presupuesto agotado antes de respuesta final]` y el usuario no recibia
        NADA. Se le da un ultimo turno con `tools=None` para que explique que
        hizo, que encontro y que falta. Una sola llamada, sin tools: no puede
        volver a entrar en el bucle.
        """
        sintetizado = self._synthesize_from_tools(messages)
        if self.router is None:
            return sintetizado
        try:
            aviso = ("[sistema] Se agoto el presupuesto de herramientas de este turno y ya no "
                     "podes llamar ninguna. Responde AHORA en texto, conciso: que hiciste, que "
                     "encontraste y que queda pendiente, con lo que ya tenes.")
            payload = self._prune_payload(messages) + [LLMMessage(role="user", content=aviso)]
            if hasattr(self.router, "chat"):
                resp = await self.router.chat(
                    payload, tools=None, hint=hint,
                    model=getattr(self, "_req_model", None),
                )
            else:
                routed = self.router.resolve(task_hint=hint)
                if routed is None:
                    return sintetizado
                resp = await routed.provider.chat(payload, tools=None, model=routed.model)
            out = (getattr(resp, "content", "") or "").strip()
            if out:
                steps.append(LoopStep(iteration=iteration, phase="decide",
                                      content=f"cierre forzado sin herramientas ({len(out)} caracteres)"))
                return out
        except Exception as e:
            logger.debug("cierre forzado fallo: %s", e)
        if sintetizado:
            return sintetizado
        # Sin resultados de herramientas no hay nada que sintetizar, pero el
        # usuario igual necesita saber qué se hizo: antes recibía solo el tag del
        # corte ("[bucle de herramienta...]") y ninguna explicación.
        hechas: list[str] = []
        for s in steps:
            _t = getattr(s, "tool_used", None)
            if _t and _t not in hechas:
                hechas.append(_t)
        if hechas:
            return ("No llegué a una respuesta final: el turno se cortó antes. "
                    f"Herramientas usadas: {', '.join(hechas[:8])}. "
                    "Probá de nuevo con un pedido más acotado (una sola cosa a la vez).")
        return ""

    async def run(self, task: str, session_id: str, hint: str | None = None, system_prompt: str | None = None, force_tools: bool = False, model: str | None = None, images: list[str] | None = None, trajectory: Trajectory | None = None, plan_mode=None, on_delta=None, on_reasoning=None, on_step=None, steer=None, extra_system: str | None = None) -> LoopResult:
        """Run TDAO loop until done, ABORT, or budget exhausted.

        Args:
            task: user task / prompt
            session_id: SessionLog session id
            hint: provider hint (simple|coding|reasoning|deep etc)
            system_prompt: optional system prompt override
            images: optional vision attachments (data URIs) on the user message
        """
        t0 = time.monotonic()
        # Perfilado del turno: cuánto se va en el LLM, en las tools y en el resto del
        # harness. Sin esto no se puede saber si un turno lento es culpa del modelo o nuestro.
        _perf_llm_ms = 0.0
        _perf_tool_ms = 0.0
        _perf_prep_ms = 0.0
        self._perf_hooks_ms = 0.0
        steps: list[LoopStep] = []
        _emitted = 0

        # Si el pedido es explícito ("meté toda esa investigación en tu grafo"),
        # el turno no está completo sin una escritura de memoria. Caso real: el
        # agente investigó, se quedó sin contexto y respondió sin guardar nada.
        _pedido = (task or "").lower()
        _pide_guardar = bool(
            re.search(r"\b(guard\w*|met[eé]\w*|aprend\w*|almacen\w*|persist\w*|"
                      r"incorpor\w*|carg\w*|memoriz\w*|anot\w*|registr\w*)\b", _pedido)
            and re.search(r"\b(grafo|memoria|brain|conocimiento|learn)\b", _pedido))
        _uso_memoria = False
        _avisado_guardar = False

        async def _flush_steps() -> None:
            """Reenvía al cliente los pasos nuevos (streaming en vivo de la trayectoria)."""
            nonlocal _emitted
            cb = getattr(self, "_on_step", None)
            if cb is None or _emitted >= len(steps):
                _emitted = len(steps)
                return
            for _s in steps[_emitted:]:
                try:
                    await cb({
                        "iteration": _s.iteration,
                        "phase": _s.phase,
                        "content": _s.content[:600],
                        "tool_used": _s.tool_used,
                        "result": (_s.result.output[:400] if _s.result else None),
                    })
                except Exception:
                    pass
            _emitted = len(steps)
        trajectory = trajectory or Trajectory(session_id=session_id, task=task)
        # Resume: cerrar turnos interrumpidos antes de emitir nada nuevo.
        try:
            trajectory.close_interrupted_turns()
        except Exception:
            pass
        total_tokens = 0
        prompt_tokens_total = 0
        completion_tokens_total = 0
        success = False
        final_output = ""
        self._empty_retries = 0  # type: ignore[attr-defined]
        self._reason_retries = 0  # type: ignore[attr-defined]
        self._retry_counts.clear()
        self._replan_counts.clear()
        self._consecutive_failures = 0  # type: ignore[attr-defined]
        self._tool_fp_window = []  # type: ignore[attr-defined]
        self._tool_repeat = {}  # type: ignore[attr-defined]
        self._tool_total = {}  # type: ignore[attr-defined]
        try:
            if self.budget is not None:
                self.budget.reset()  # reset per-run (budget persiste en instancias reusadas)
        except Exception:
            pass
        self._req_model = model  # type: ignore[attr-defined]
        self._resp_model_last = ""  # type: ignore[attr-defined]
        self._on_delta = on_delta  # type: ignore[attr-defined]
        self._on_reasoning = on_reasoning  # type: ignore[attr-defined]
        self._on_step = on_step  # type: ignore[attr-defined]
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
        # Ventana REAL del modelo de este turno. Sin esto la compactacion asume un
        # numero fijo (session.max_tokens): compacta tarde si el modelo tiene menos
        # ventana (Ollama responde 400 y el turno se cae) o de gusto si tiene mas.
        self._model_ctx_tokens: int = 0  # type: ignore[attr-defined]
        await self._resolve_model_context_window(hint)

        # Init trajectory + session
        trajectory.append_user(task, iteration=0)
        if self.session_log is not None:
            try:
                from .session_log import Message
                self.session_log.append(session_id, Message(role="user", content=task))
                # Título de la sesión = primer pedido del usuario (como opencode), sin
                # markdown ni saltos: la barra lateral mostraba la respuesta o el resumen.
                try:
                    _sess = self.session_log.get_session(session_id)
                    if _sess is not None and not str(getattr(_sess, "title", "") or "").strip():
                        _t = " ".join(task.strip().split())
                        _t = _t.replace("**", "").replace("`", "").replace("#", "").strip()
                        if _t:
                            self.session_log.update_session(session_id, title=_t[:80])
                except Exception:
                    pass
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
                "Si falta un especialista, creálo con agent_create (plantillas scout/builder/reviewer/"
                "qa/researcher/ops y modo read_only/build). No invoques herramientas innecesarias. Sé conciso. "
                "Si el pedido es una TAREA (analizar, crear, arreglar, escribir un programa), hacela "
                "hasta el final con las herramientas. El entregable es lo que el usuario va a usar: si "
                "construis algo, no lo des por terminado hasta que ese camino funcione de punta a punta "
                "y lo hayas probado vos mismo — una pieza interna correcta (un modulo, una capa de "
                "protocolo, un test) no reemplaza al programa andando. No devuelvas un plan ni preguntes "
                "si avanzar cuando ya tenes con que hacerlo, y no cierres con una lista de pendientes: "
                "segui trabajando hasta que ande. Solo si quedas bloqueado por algo externo (credencial, "
                "permiso, servicio caido) deci exactamente que y por que."
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

        # Skills instaladas: en vez de volcar el catalogo entero (miles de SKILL.md
        # no entran en el prompt), se listan solo las que se parecen a la tarea y
        # se ensena a buscar el resto con la tool `skill`.
        try:
            from src.core.skills_catalog import hint_block as _skills_hint
            _shint = _skills_hint(task)
            if _shint and messages and messages[0].role == "system":
                messages[0] = LLMMessage(role="system", content=(messages[0].content or "") + _shint)
        except Exception:
            pass

        # Memoria proactiva: el agente arranca sabiendo lo que ya se aprendio.
        # Sin esto cada sesion empieza de cero: repite errores ya resueltos o
        # concluye "no esta disponible" cuando el grafo tiene la respuesta.
        try:
            # El server ya inyecta su bloque (memory_context, con rerank y
            # telemetria); esto cubre a los loops que corren sin server (MCP,
            # ACP, agent_run). Sin la guarda el prompt llevaba dos bloques de
            # memoria con el mismo titulo y el modelo no sabia cual mandaba.
            _sys_actual = (messages[0].content or "") if messages and messages[0].role == "system" else ""
            # extra_system (la memoria del server) se concatena mas abajo: hay que
            # mirarlo tambien o el prompt termina con los dos bloques igual.
            _ya_hay_memoria = "MEMORIA DEL GRAFO (recuperada" in (_sys_actual + str(extra_system or ""))
            if not _ya_hay_memoria:
                _pk = await self._prior_knowledge_block(task)
                if _pk and messages and messages[0].role == "system":
                    messages[0] = LLMMessage(role="system", content=_sys_actual + _pk)
        except Exception as e:
            logger.debug("memoria proactiva no disponible: %s", e)

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
        # Request pinneada: al reanudar hay que mandar el MISMO prompt y el MISMO
        # set de tools que el turno anterior. Re-armarlo con el estado de hoy
        # (AGENTS.md editado, skill nueva, tool desregistrada) cambia el contexto
        # a mitad de sesión y el modelo ve algo distinto al que venía usando.
        _pin: dict | None = None
        _use_pin = False
        if self.session_log is not None and session_id:
            try:
                _pinned_msgs = self.session_log.count_messages(session_id)
                _pin = self.session_log.get_pin(session_id)
                _use_pin = bool(_pin and _pinned_msgs > 1 and not system_prompt)
            except Exception:
                _pin = None
                _use_pin = False
        if _use_pin and messages and messages[0].role == "system":
            messages[0] = LLMMessage(role="system", content=_pin["system_prompt"])

        # Estado del agente (harness): va DESPUÉS del pin a propósito. El prompt
        # pineado es la forma del request y no cambia; la memoria del agente sí,
        # y tiene que reflejar lo que se creó en el turno anterior.
        try:
            from src.core.harness_state import digest_block as _harness_digest
            _hblk = _harness_digest(session_id)
            if _hblk and messages and messages[0].role == "system":
                messages[0] = LLMMessage(role="system", content=(messages[0].content or "") + _hblk)
        except Exception as e:
            logger.debug(f"digest del harness falló: {e}")

        # Load history from session_log if available
        history_limit = 20
        if self.session_log is not None:
            try:
                # Resumen durable: si existe, el historial arranca DESPUES del
                # mensaje de resumen y este se inyecta como system. Sin esto,
                # tras un reinicio se recargaba la cola cruda y el resumen se
                # perdia (compactacion efimera).
                _summary_msg = None
                try:
                    _summary_msg = self.session_log.get_summary_message(session_id)
                except Exception:
                    _summary_msg = None
                if _summary_msg:
                    _from = self.session_log.get_messages_from(
                        session_id, int(_summary_msg["id"]), limit=10000)
                    _tail = _from[-history_limit:]
                    _sc = str(_summary_msg.get("content") or "").strip()
                    if _sc:
                        _pref = str(getattr(self.compaction, "handoff_prefix", "") or "")
                        _content = f"{_pref}\n{_sc}" if _pref else _sc
                        messages.insert(1, LLMMessage(role="system", content=_content))
                else:
                    # Ventana de los 20 mensajes MAS RECIENTES: get_messages es ASC,
                    # asi que hay que paginar desde el final — con limit asecas se
                    # cargaban los 20 mas viejos de la sesion y se perdia el
                    # contexto reciente (capa 2, contaminacion/olvido).
                    _total = self.session_log.count_messages(session_id)
                    _off = max(0, _total - history_limit)
                    _tail = [
                        m.to_dict()
                        for m in self.session_log.get_messages(
                            session_id, limit=history_limit, offset=_off)
                    ]
                # Skip the just-appended user message duplicate: la tarea actual
                # se agrega al final, fresca, mas abajo.
                messages.extend(_replay_history(_tail[:-1]))
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
        _compactaciones = 0
        if self.compaction is not None:
            try:
                dict_msgs = self._to_dicts(messages)
                # Use router to decide if we need compaction via token estimate.
                # Presupuesto corregido por el usage real del provider (Cline).
                max_tokens = self._effective_context_max_tokens(session_id)
                if self._should_compact(session_id, dict_msgs, max_tokens):
                    _compactaciones += 1
                    await self._distill(session_id)
                    _limit = int(max_tokens * self.compact_threshold)
                    _prev, _progress = self._summary_state(session_id)
                    if getattr(self, "router", None) is not None and hasattr(self.compaction, "summarize_with_llm"):
                        dict_msgs, cmetrics = await self.compaction.summarize_with_llm(
                            dict_msgs, _limit, self.router,
                            previous_summary=_prev, progress_messages=_progress,
                            session_id=session_id)
                    else:
                        dict_msgs, cmetrics = await self.compaction.compact(
                            dict_msgs, _limit, session_id=session_id)
                    self._note_compaction(session_id, cmetrics, dict_msgs)
                    self._persist_compaction_summary(session_id, dict_msgs)
                    self._persist_compaction_state(session_id, cmetrics, dict_msgs)
                    await self._emit_compact_hook(
                        session_id, cmetrics, self._compaction_summary(dict_msgs))
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

        if _use_pin:
            openai_tools = _tools_from_pin(openai_tools, _pin)
        elif self.session_log is not None and session_id and messages:
            try:
                self.session_log.pin_request(
                    session_id, messages[0].content or "", openai_tools or [],
                    model=str(model or ""), force=True)
            except Exception as e:
                logger.debug(f"pin de la request falló: {e}")

        # Los schemas de las tools viajan en CADA request y no estaban contados:
        # con 53 tools son ~7.3k tokens, asi que el arnes se creia 4x mas holgado,
        # compactaba a los 6.6k de 24k y el resumen (800 tokens) se comia la lista
        # de tools que el agente acababa de pedir.
        self._tools_tokens_n = self._count_tools_tokens(openai_tools)  # type: ignore[attr-defined]

        # Main loop
        iteration = 0
        _persistio_final = False
        max_iters = self.budget.max_iterations
        # Config error policy
        error_retry_limit = 3
        error_replan_limit = 2
        if hasattr(self, "_error_policy"):
            error_retry_limit = self._error_policy.get("retry", 3)
            error_replan_limit = self._error_policy.get("replan", 2)

        _perf_setup_ms = (time.monotonic() - t0) * 1000.0
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
                _t_prep = time.monotonic()
                if self.hooks is not None and self.hooks.has_handlers("pre_llm_call"):
                    await self.hooks.emit("pre_llm_call", {"iteration": iteration, "messages": len(messages), "hint": hint})
                # Poda SOLO el payload: cabeza+cola de tool results viejos.
                _payload = self._sanear_historial(self._prune_payload(messages))
                _request_estimate = 0
                if self.compaction is not None:
                    try:
                        _request_estimate = self.compaction.estimate_tokens_for_messages(
                            self._to_dicts(_payload)) + int(getattr(self, "_tools_tokens_n", 0) or 0)
                    except Exception:
                        _request_estimate = 0
                _perf_prep_ms += (time.monotonic() - _t_prep) * 1000.0
                # Router.chat con fallback (o streaming token-a-token si hay on_delta)
                _t_llm = time.monotonic()
                if getattr(self, "_on_delta", None) is not None and hasattr(self.router, "stream"):
                    llm_resp = await self._stream_turn(_payload, openai_tools, hint, force_tools, require_vision=bool(images))
                elif hasattr(self.router, "chat"):
                    llm_resp = await self.router.chat(_payload, tools=openai_tools, hint=hint, force_tools=force_tools, model=getattr(self, "_req_model", None), require_vision=bool(images))
                else:
                    # Fallback: resolve then chat
                    routed = self.router.resolve(task_hint=hint)
                    if routed is None:
                        raise RuntimeError(f"sin proveedor para pista={hint}")
                    llm_resp = await routed.provider.chat(_payload, tools=openai_tools, model=routed.model)
                _perf_llm_ms += (time.monotonic() - _t_llm) * 1000.0
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
                            from src.core import context_limits as _ctxl

                            # Aprender el limite REAL del error: sin esto el turno
                            # reintenta con el mismo tamano y vuelve a fallar. Si el
                            # error no trae numero (Ollama), se baja la ventana a la
                            # mitad para que la proxima compactacion llegue antes.
                            _model = getattr(self, "_req_model", "") or ""
                            _real = _ctxl.limit_from_error(err_str)
                            if _real and _model:
                                _ctxl.register(_model, _real)
                            _prev_win = int(getattr(self, "_model_ctx_tokens", 0) or 0) or int(self.context_max_tokens)
                            self._model_ctx_tokens = int(min(_prev_win, _real or max(1024, _prev_win // 2)))
                            logger.warning(
                                "context: overflow en %s; ventana asumida %d -> %d (limite del error: %s)",
                                _model or "modelo", _prev_win, self._model_ctx_tokens, _real or "sin dato",
                            )
                            dict_msgs = self._to_dicts(messages)
                            # El resumen de emergencia tiene que entrar en la ventana nueva.
                            dict_msgs = await self.compaction.reactive_compact(
                                dict_msgs, err_str, limit=self._model_ctx_tokens,
                                session_id=session_id,
                            )
                            messages = self._rebuild_messages(dict_msgs)
                            await _close_turn("compact-retry")
                            continue  # retry LLM call with compacted context
                        except Exception as e:
                            logger.debug(f"reactive compact falló: {e}")
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
            # Calibracion: el provider cuenta el prompt real (incluye tools y
            # overhead). Si supera nuestra estimacion, el proximo presupuesto
            # se divide por ese factor (cap 4x) para compactar a tiempo.
            _prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
            if _prompt_tokens > 0 and _request_estimate > 0:
                record_usage_calibration(session_id, _request_estimate, _prompt_tokens)
            if not _completion:
                _completion = len((llm_resp.content or "")) // 4
            turn_tokens = int(usage.get("total_tokens", 0) or _completion)
            total_tokens += turn_tokens
            prompt_tokens_total += _prompt_tokens
            completion_tokens_total += _completion
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
            if _resp_model:
                self._resp_model_last = _resp_model  # type: ignore[attr-defined]
            if llm_resp.has_tool_calls:
                for tc in llm_resp.tool_calls:
                    trajectory.append_assistant(assistant_content, iteration=iteration, tool_name=tc.name, tool_call_id=tc.id, tool_args=tc.arguments, turn=iteration, step="assistant", tokens=turn_tokens, model=_resp_model)
            else:
                trajectory.append_assistant(assistant_content, iteration=iteration, turn=iteration, step="assistant", tokens=turn_tokens, model=_resp_model)

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

            # Session log: se persiste también el assistant que solo emitió
            # tool_calls (sin texto). Antes se descartaba para no dejar burbujas
            # vacías, y al reanudar el resultado de la tool no tenía llamada que
            # lo empareje: el historial se degradaba a notas y la cadena no se
            # podía continuar. La UI filtra la fila si no hay texto.
            if self.session_log is not None and ((assistant_content or "").strip() or _tcs):
                try:
                    from .session_log import Message
                    self.session_log.append(session_id, Message(
                        role="assistant",
                        content=assistant_content or "",
                        model=getattr(llm_resp, "model", ""),
                        tokens=turn_tokens,
                        tool_calls=_wire_tool_calls(_tcs),
                    ))
                except Exception:
                    pass

            if not _tcs:
                # Cierre sin haber guardado lo que el usuario pidió guardar: una
                # sola vez se le recuerda, con las herramientas todavía activas.
                if (_pide_guardar and not _uso_memoria and not _avisado_guardar
                        and self.tools is not None and self.tools.get("learn") is not None):
                    _avisado_guardar = True
                    messages.append(LLMMessage(role="user", content=(
                        "[recordatorio del sistema] El pedido incluye guardar lo investigado en el "
                        "grafo y todavía no llamaste a 'learn'. Guardá lo que ya tenés (una entrada "
                        "por tema, con la fuente) antes de responder; no vuelvas a investigar.")))
                    steps.append(LoopStep(iteration=iteration, phase="decide",
                                          content="recordatorio: falta guardar en el grafo"))
                    await _flush_steps()
                    continue
                # Un steer que llega mientras el modelo redacta la respuesta no
                # puede perderse: la UI lo manda cuando el run esta en curso y, si
                # el turno cierra aca, el mensaje queda en el historial sin que
                # nadie lo conteste (paso con "debes hacer funcionar rea, solucionalo":
                # se guardo como [124] y el turno siguio hablando de otra cosa).
                if callable(steer):
                    try:
                        _tarde = [s for s in (steer() or []) if s]
                    except Exception:
                        _tarde = []
                    if _tarde:
                        for _s in _tarde:
                            messages.append(LLMMessage(role="user", content=f"[steer del usuario] {_s}"))
                            try:
                                await self._emit_cycle_async(trajectory, "steer", iteration, content=str(_s)[:500])
                            except Exception:
                                pass
                        steps.append(LoopStep(iteration=iteration, phase="decide",
                                              content="steer tardio: se responde antes de cerrar el turno"))
                        await _flush_steps()
                        continue
                # Final answer
                steps.append(LoopStep(iteration=iteration, phase="decide", content=f"respuesta final ({len(assistant_content)} caracteres)"))
                final_output = assistant_content
                success = True
                _persistio_final = True
                await _close_turn("stop")
                break
            # ---- ACT: pre-dispatch paralelo de herramientas de SOLO LECTURA ----
            # Solo se solapa un PREFIJO CONTIGUO de tools de lectura. Una tool
            # mutante es una barrera: si el batch es [write, read], el read NO
            # puede ejecutarse antes del write (Cline: executionMode + grupos
            # adyacentes). Antes se pre-despachaban todas las read-only del batch
            # aunque estuvieran después de una mutante, reordenando efectos.
            _pre: dict[int, Any] = {}
            try:
                if len(_tcs) > 1 and _parallel_tools_on() and self.tools is not None:
                    from .concurrency import gather_limited
                    _safe = []
                    for _i, _tc in enumerate(_tcs):
                        _n = (_tc.name or "").lower()
                        if _n not in _READONLY_TOOLS:
                            break  # barrera: lo que sigue corre serial, en orden
                        _a = _tc.arguments
                        if isinstance(_a, str):
                            try:
                                _a = json.loads(_a) if _a else {}
                            except Exception:
                                _a = {}
                        _safe.append((_i, _tc.name or _n, _a if isinstance(_a, dict) else {}))
                    if len(_safe) > 1:
                        _res = await gather_limited([
                            self.tools.dispatch(n, a, scope=self.scope, hooks=self.hooks,
                                                approval=self.approval, session_id=session_id)
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
                await _flush_steps()

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
                    if _n in self.anti_loop_repeat_warn:
                        try:
                            messages.append(LLMMessage(role="user", content=(
                                f"[recordatorio del sistema] repetiste la herramienta '{tool_name}' {_n} veces con "
                                f"los MISMOS argumentos y no avanzás. CAMBIÁ de estrategia (otros args, otra tool) "
                                f"o da tu respuesta final con lo que ya tenés.")))
                        except Exception:
                            pass
                    elif _n >= self.anti_loop_repeat_stop:
                        final_output = f"[doom-loop: {tool_name} repetido {_n} veces con mismos args — bucle detenido; sin respuesta final]"
                        success = False
                        iteration = max_iters  # force exit
                        break
                except Exception:
                    pass
                # Anti-bucle por HERRAMIENTA (aunque cambien los argumentos): leer los
                # archivos de a uno hasta quedarse sin presupuesto no es "doom loop"
                # técnico pero tampoco progresa. Avisa y, si insiste, corta.
                try:
                    _tot = getattr(self, "_tool_total", None)
                    if _tot is None:
                        _tot = {}
                        self._tool_total = _tot  # type: ignore[attr-defined]
                    # `mcp_call` es el transporte de 100+ tools distintas: contar
                    # llamadas globales corta trabajo real (un analisis de binario
                    # hace 10-20 mcp_call seguidos, cada una una tool diferente). El
                    # bucle de verdad es la MISMA tool MCP con los MISMOS args, y eso
                    # ya lo detecta `_fp` arriba.
                    _clave = tool_name
                    if tool_name == "mcp_call" and isinstance(tool_args, dict):
                        _clave = f"mcp_call:{tool_args.get('server') or ''}"
                    _tn = _tot.get(_clave, 0) + 1
                    _tot[_clave] = _tn
                    # Transportes genericos: `mcp_call` lleva a 100+ tools y `repl`/`exec`
                    # llevan cualquier codigo. Contarlos como "la misma herramienta"
                    # corta trabajo real (una sesion de depuracion hace 10-20 repl
                    # seguidos, cada uno con codigo distinto). El bucle de verdad
                    # -mismos args- ya lo detecta `_fp`. Limites altos y aviso claro.
                    _transportes = {"mcp_call": (40, 80), "repl": (40, 120),
                                    "exec": (40, 120), "run_command": (40, 120)}
                    if tool_name in _transportes:
                        _warn, _stop = _transportes[tool_name]
                    else:
                        _warn, _stop = self.anti_loop_tool_warn, self.anti_loop_tool_stop
                    if _tn == _warn:
                        if tool_name in ("job_read", "job_wait"):
                            # Polling en incrementos minimos fue el bucle real
                            # observado: job_read(cursor=0), luego cursor=100, 200...
                            # El aviso tiene que ensenar el uso correcto del cursor.
                            _aviso = (
                                f"[recordatorio del sistema] ya llamaste '{tool_name}' {_tn} veces en "
                                "este turno. Para leer un job no hace falta pedir tramos chicos: pasa "
                                "como `cursor` el `next_cursor` que devolvio la lectura anterior, subi "
                                "`max_chars` (hasta 200000) o usa `job_wait` con ese mismo `cursor` para "
                                "recibir todo junto cuando el proceso termine. Si ya tenes lo necesario, "
                                "responde con eso.")
                        elif tool_name == "mcp_call":
                            _aviso = (
                                f"[recordatorio del sistema] ya hiciste {_tn} llamadas MCP en este turno. "
                                "Si no estas avanzando, deja de explorar: anota lo obtenido, usa lo que ya "
                                "sabes y empeza a producir (escribir archivos, correr lo que creaste). No "
                                "repitas la misma tool con los mismos argumentos.")
                        else:
                            _aviso = (
                                f"[recordatorio del sistema] ya llamaste '{tool_name}' {_tn} veces en este "
                                "turno. Si con esta herramienta no estas llegando al objetivo, cambia de "
                                "enfoque: usa una via mas directa (glob para listar, exec para contar o "
                                "resumir en un comando, code_search para buscar) en vez de repetir la misma "
                                "herramienta, y responde con lo que ya tengas.")
                        messages.append(LLMMessage(role="user", content=_aviso))
                    elif _tn >= _stop:
                        final_output = (
                            f"[bucle de herramienta: '{tool_name}' llamada {_tn} veces sin llegar a una "
                            "respuesta final — turno detenido para no agotar el presupuesto]")
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
                        _t_tool = time.monotonic()
                        raw_result = await self.tools.dispatch(tool_name, tool_args, scope=self.scope, hooks=self.hooks,
                                                               approval=self.approval, session_id=session_id)
                        _tool_ms = (time.monotonic() - _t_tool) * 1000.0
                        _perf_tool_ms += _tool_ms
                        try:
                            logger.info("tool perf: %s %dms", tool_name, int(_tool_ms))
                        except Exception:
                            pass
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
                    if tool_name in ("learn", "brain_remember", "memory_set", "knowledge_add"):
                        _uso_memoria = True
                    # Formateo-in-the-loop: dejar el archivo prolijo ANTES de
                    # diagnosticar, para que el linter opine sobre el texto final
                    # y el checkpoint guarde un diff sin ruido.
                    try:
                        if tool_name in ("write_file", "edit", "apply_patch") and isinstance(tool_args, dict):
                            _fmt_path = tool_args.get("path")
                            if _fmt_path:
                                from .formatters import format_file
                                _fmt = format_file(_fmt_path, cfg=getattr(self, "_cfg", None) or None)
                                _st = _fmt.get("status")
                                if _st == "formatted":
                                    tool_result_str = (tool_result_str or "") + f"\n[formateado con {_fmt.get('formatter') or 'formateador'}]"
                                elif _st == "error":
                                    _d = str(_fmt.get("detail") or "")[:200]
                                    tool_result_str = (tool_result_str or "") + f"\n[formateo falló ({_fmt.get('formatter') or _fmt_path}): {_d}]"
                    except Exception:
                        pass
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
                            tool_result_str += " [presupuesto replanificación agotado]"
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
                    if _cf >= self.anti_loop_failures_stop:
                        final_output = (
                            f"[abortado: {_cf} fallos consecutivos de herramientas; "
                            f"último: {tool_name} -> {tool_result_str[:300]}]")
                        success = False
                        iteration = max_iters
                        break
                    if _cf == self.anti_loop_failures_warn:
                        messages.append(LLMMessage(role="user", content=(
                            f"[recordatorio del sistema] {_cf} fallos consecutivos de herramientas. "
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

                # Anti-bucle por RESULTADO repetido. Repetir la misma herramienta
                # con argumentos distintos es legítimo (leer 9 archivos), pero si
                # la salida es idéntica no hay progreso: el caso real fue
                # `systeminfo | findstr`, que devolvía la misma línea y el modelo
                # la pidió 10 veces hasta el corte por herramienta.
                try:
                    _txt = (tool_result_str or "").strip()
                    _firma = ""
                    if len(_txt) >= 40:
                        _firma = hashlib.sha256(
                            f"{tool_name}\x00{_txt}".encode("utf-8", "replace")
                        ).hexdigest()[:16]
                    if _firma:
                        _rep = getattr(self, "_res_repeat", None)
                        if _rep is None:
                            _rep = {}
                            self._res_repeat = _rep  # type: ignore[attr-defined]
                        _rn = _rep.get(_firma, 0) + 1
                        _rep[_firma] = _rn
                        if _rn == 2:
                            messages.append(LLMMessage(role="user", content=(
                                f"[recordatorio del sistema] '{tool_name}' devolvió EXACTAMENTE el mismo "
                                "resultado otra vez: no hay avance. No la repitas con un comando "
                                "equivalente; responde con lo que ya tenés o cambiá de herramienta.")))
                        elif _rn >= 3:
                            final_output = (
                                f"[bucle de herramienta: '{tool_name}' devolvió el mismo resultado "
                                f"{_rn} veces sin avanzar — turno detenido]")
                            success = False
                            iteration = max_iters  # force exit
                            break
                except Exception:
                    pass

            # ---- OBSERVE: budget check + compaction ----
            await _flush_steps()
            if self.budget.exhausted:
                steps.append(LoopStep(iteration=iteration, phase="observe", content=f"presupuesto agotado tras iter {iteration}: {self.budget.reason}"))
                # El placeholder se reemplaza por un cierre en texto al salir del
                # loop (un solo camino para los tres cortes: presupuesto, doom-loop
                # y bucle por herramienta).
                final_output = final_output or "[presupuesto agotado antes de respuesta final]"
                await _close_turn("budget")
                break

            # Maybe compact if approaching limit
            if self.compaction is not None and len(messages) > 10:
                try:
                    dict_msgs = self._to_dicts(messages)
                    max_tokens = self._effective_context_max_tokens(session_id)
                    # Una sola compactación por turno. Con `minimum_progress_messages=2`
                    # cada par de resultados volvía a disparar el resumen, y como el
                    # resumen previo se re-resumía, el pedido original se degradaba
                    # ("pendiente: confirmar si AGENTS.md existe") hasta perderlo. Si
                    # tras compactar sigue al límite, el problema es la ventana del
                    # modelo, no el historial: se deja al provider fallar con datos
                    # reales en vez de resumir en cascada.
                    if _compactaciones == 0 and self._should_compact(session_id, dict_msgs, max_tokens):
                        _compactaciones += 1
                        await self._distill(session_id)
                        # Solo compactar system+historial, mantener resultados de herramientas recientes
                        _limit = int(max_tokens * self.compact_threshold)
                        _prev, _progress = self._summary_state(session_id)
                        if getattr(self, "router", None) is not None and hasattr(self.compaction, "summarize_with_llm"):
                            dict_msgs, _cmetrics = await self.compaction.summarize_with_llm(
                                dict_msgs, _limit, self.router,
                                previous_summary=_prev, progress_messages=_progress,
                                session_id=session_id)
                        else:
                            dict_msgs, _cmetrics = await self.compaction.compact(
                                dict_msgs, _limit, session_id=session_id)
                        self._note_compaction(session_id, _cmetrics, dict_msgs)
                        self._persist_compaction_summary(session_id, dict_msgs)
                        self._persist_compaction_state(session_id, _cmetrics, dict_msgs)
                        await self._emit_compact_hook(
                            session_id, _cmetrics, self._compaction_summary(dict_msgs))
                        messages = self._rebuild_messages(dict_msgs)
                        steps.append(LoopStep(iteration=iteration, phase="observe", content=f"contexto compactado ({len(dict_msgs)} mensajes)"))
                except Exception as e:
                    logger.debug(f"observe compaction falló: {e}")
            # Fin de turno con herramientas: se cierra y el while abre el siguiente.
            await _close_turn("tool_calls")

        # Finalize
        _perf_final_start = time.monotonic()
        await _flush_steps()
        duration_ms = (time.monotonic() - t0) * 1000
        try:
            _final_ms = (time.monotonic() - _perf_final_start) * 1000.0
            logger.info(
                "loop perf: iter=%d llm=%dms tools=%dms prep=%dms setup=%dms final=%dms hooks=%dms resto=%dms total=%dms",
                iteration, int(_perf_llm_ms), int(_perf_tool_ms), int(_perf_prep_ms),
                int(_perf_setup_ms), int(_final_ms), int(getattr(self, "_perf_hooks_ms", 0.0)),
                int(max(0.0, duration_ms - _perf_llm_ms - _perf_tool_ms - _perf_prep_ms - _perf_setup_ms - _final_ms - getattr(self, "_perf_hooks_ms", 0.0))),
                int(duration_ms),
            )
        except Exception:
            pass
        # Un turno cortado (presupuesto agotado, doom-loop, bucle por herramienta, o
        # la salida por iteraciones cuando los reintentos de razonamiento/vacio
        # consumieron el presupuesto con `continue`) no puede terminar en un
        # placeholder NI quedar vacio: el usuario pidio algo y recibe "[...]" o "".
        # Se pide un cierre en texto, SIN herramientas, con lo ya averiguado. El
        # diagnostico del bucle se conserva (es honesto sobre por que se corto) y
        # el exito sigue siendo False para los bucles.
        _placeholder = ("[presupuesto agotado", "[doom-loop", "[bucle de herramienta")
        if (not final_output) or final_output.startswith(_placeholder):
            _cierre = await self._final_wrapup(messages, hint, steps, iteration)
            _es_bucle = final_output.startswith(("[doom-loop", "[bucle de herramienta"))
            if _cierre:
                if _es_bucle:
                    final_output = final_output + "\n\n" + _cierre
                else:
                    final_output = _cierre
                    success = True
            elif not final_output:
                final_output = "[presupuesto agotado antes de respuesta final]"

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
        # Un corte no se persistía: el assistant con tool_calls queda en la
        # sesión, pero el texto del cierre (bucle, presupuesto) solo viajaba por
        # el stream. Al reabrir la sesión el usuario veía la última tool y ninguna
        # explicación. Se guarda el cierre una sola vez.
        if final_output and final_output.strip() and not _persistio_final and self.session_log is not None:
            try:
                from .session_log import Message
                self.session_log.append(session_id, Message(role="assistant", content=final_output))
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
            prompt_tokens=prompt_tokens_total,
            completion_tokens=completion_tokens_total,
            duration_ms=duration_ms,
            trajectory=trajectory,
            model=self._resp_model_last or self._req_model or "",
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
            # "secuencia MEDIUM" / "requiere aprobacion" NO son el scope: son el
            # gate de aprobaciones. Decirle "revisá las raíces permitidas" al
            # modelo lo mandaba a investigar permisos en bucle mientras el
            # problema era otro.
            if "secuencia" in low or "aprobacion" in low or "approval" in low:
                hint = ("no es el scope: lo frenó el gate de aprobaciones (o no hay canal "
                        "para aprobar). No repitas la misma llamada; si el contenido "
                        "menciona secretos (.env, keys, credentials), escribilo sin esas "
                        "menciones o pedile al usuario que lo apruebe en Aprobaciones.")
            else:
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
