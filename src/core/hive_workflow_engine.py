"""
Hive Workflow Engine — Persistent DAG orchestrator over the NexusHive message board.

Builds on top of `NexusHiveBridge` (message_board.db) to coordinate multi-agent
workflows. Steps are dispatched to peers (gemas, peer agents, autonomous loops)
via the message board; their `task_done` responses are correlated back through
`metadata.workflow_id / step_id / task_id` keys. Workflow state is persisted in
the same SQLite DB so any process can resume an interrupted run.

Public surface:
    WorkflowDef, Workflow, WorkflowStep, StepStatus, StepKind
    HiveWorkflowEngine, WorkflowRegistry, WorkflowTemplate
    WorkflowEvent (event log records)

Usage:
    engine = HiveWorkflowEngine(agent_name="director")
    reg = WorkflowRegistry(engine)
    reg.register("research_pipeline", WORKFLOW_DEF)
    handle = engine.start("research_pipeline", inputs={"topic": "AI agents"})
    result = engine.wait(handle.id, timeout=600)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Union

from src.integrations.nexus_hive_bridge import NexusHiveBridge

logger = logging.getLogger("nexus.hive_workflow")


DEFAULT_DB = Path.home() / ".nexus" / "brain" / "message_board.db"


class StepStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    WAITING = "waiting"


class StepKind(str, Enum):
    AGENT = "agent"
    PARALLEL = "parallel"
    CONDITION = "condition"
    WAIT = "wait"
    HUMAN = "human"
    BUILTIN = "builtin"


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"


@dataclass
class StepResult:
    status: StepStatus
    output: Any = None
    error: Optional[str] = None
    raw_content: str = ""
    response_metadata: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    attempts: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "output": self.output,
            "error": self.error,
            "raw_content": self.raw_content,
            "response_metadata": self.response_metadata,
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StepResult":
        return cls(
            status=StepStatus(d.get("status", "completed")),
            output=d.get("output"),
            error=d.get("error"),
            raw_content=d.get("raw_content", ""),
            response_metadata=d.get("response_metadata", {}) or {},
            duration_ms=int(d.get("duration_ms", 0) or 0),
            attempts=int(d.get("attempts", 1) or 1),
        )


@dataclass
class WorkflowStep:
    id: str
    kind: StepKind
    title: str = ""
    description: str = ""
    target_agent: Optional[str] = None
    prompt: str = ""
    depends_on: List[str] = field(default_factory=list)
    timeout_s: float = 300.0
    max_retries: int = 1
    retry_backoff_s: float = 2.0
    condition_expr: Optional[str] = None
    parallel_children: List["WorkflowStep"] = field(default_factory=list)
    builtin_fn: Optional[str] = None
    builtin_args: Dict[str, Any] = field(default_factory=dict)
    wait_predicate: Optional[str] = None
    channel: str = "general"
    metadata: Dict[str, Any] = field(default_factory=dict)
    on_error: str = "fail"
    result: Optional[StepResult] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "title": self.title,
            "description": self.description,
            "target_agent": self.target_agent,
            "prompt": self.prompt,
            "depends_on": list(self.depends_on),
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "retry_backoff_s": self.retry_backoff_s,
            "condition_expr": self.condition_expr,
            "parallel_children": [c.to_dict() for c in self.parallel_children],
            "builtin_fn": self.builtin_fn,
            "builtin_args": self.builtin_args,
            "wait_predicate": self.wait_predicate,
            "channel": self.channel,
            "metadata": self.metadata,
            "on_error": self.on_error,
            "result": self.result.to_dict() if self.result else None,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowStep":
        kind = StepKind(d.get("kind", "agent"))
        result = StepResult.from_dict(d["result"]) if d.get("result") else None
        return cls(
            id=d["id"],
            kind=kind,
            title=d.get("title", ""),
            description=d.get("description", ""),
            target_agent=d.get("target_agent"),
            prompt=d.get("prompt", ""),
            depends_on=list(d.get("depends_on", []) or []),
            timeout_s=float(d.get("timeout_s", 300.0) or 300.0),
            max_retries=int(d.get("max_retries", 1) or 1),
            retry_backoff_s=float(d.get("retry_backoff_s", 2.0) or 2.0),
            condition_expr=d.get("condition_expr"),
            parallel_children=[cls.from_dict(c) for c in d.get("parallel_children", []) or []],
            builtin_fn=d.get("builtin_fn"),
            builtin_args=d.get("builtin_args", {}) or {},
            wait_predicate=d.get("wait_predicate"),
            channel=d.get("channel", "general"),
            metadata=d.get("metadata", {}) or {},
            on_error=d.get("on_error", "fail"),
            result=result,
        )


@dataclass
class WorkflowDef:
    name: str
    description: str = ""
    version: str = "1.0"
    steps: List[WorkflowStep] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    inputs_schema: Dict[str, Any] = field(default_factory=dict)
    default_timeout_s: float = 1800.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_by: str = "system"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "steps": [s.to_dict() for s in self.steps],
            "tags": list(self.tags),
            "inputs_schema": self.inputs_schema,
            "default_timeout_s": self.default_timeout_s,
            "metadata": self.metadata,
            "created_by": self.created_by,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "WorkflowDef":
        return cls(
            name=d["name"],
            description=d.get("description", ""),
            version=d.get("version", "1.0"),
            steps=[WorkflowStep.from_dict(s) for s in d.get("steps", []) or []],
            tags=list(d.get("tags", []) or []),
            inputs_schema=d.get("inputs_schema", {}) or {},
            default_timeout_s=float(d.get("default_timeout_s", 1800.0) or 1800.0),
            metadata=d.get("metadata", {}) or {},
            created_by=d.get("created_by", "system"),
        )

    def index(self) -> Dict[str, WorkflowStep]:
        out: Dict[str, WorkflowStep] = {}
        for s in self.steps:
            out[s.id] = s
            for c in s.parallel_children:
                out[c.id] = c
        return out

    def validate(self) -> List[str]:
        errors: List[str] = []
        seen: Set[str] = set()
        for s in self.steps:
            if s.id in seen:
                errors.append(f"duplicate step id: {s.id}")
            seen.add(s.id)
            for dep in s.depends_on:
                if dep not in seen and dep not in {c.id for c in self._all_descendants(s)}:
                    errors.append(f"step {s.id} depends on unknown step {dep}")
        for s in self.steps:
            errors.extend(self._validate_step(s, seen))
        if not self.steps:
            errors.append("workflow has no steps")
        return errors

    def _all_descendants(self, s: WorkflowStep) -> List[WorkflowStep]:
        out: List[WorkflowStep] = []
        stack = list(s.parallel_children)
        while stack:
            n = stack.pop()
            out.append(n)
            stack.extend(n.parallel_children)
        return out

    def _validate_step(self, s: WorkflowStep, _seen: Set[str]) -> List[str]:
        errs: List[str] = []
        if s.kind == StepKind.AGENT and not s.target_agent:
            errs.append(f"step {s.id} (agent) missing target_agent")
        if s.kind == StepKind.BUILTIN and not s.builtin_fn:
            errs.append(f"step {s.id} (builtin) missing builtin_fn")
        if s.kind == StepKind.CONDITION and not s.condition_expr:
            errs.append(f"step {s.id} (condition) missing condition_expr")
        if s.kind == StepKind.WAIT and not s.wait_predicate:
            errs.append(f"step {s.id} (wait) missing wait_predicate")
        if s.on_error not in ("fail", "skip", "continue", "retry"):
            errs.append(f"step {s.id} on_error must be fail|skip|continue|retry")
        if s.max_retries < 0 or s.timeout_s <= 0:
            errs.append(f"step {s.id} invalid retry/timeout values")
        return errs


@dataclass
class Workflow:
    id: str
    def_name: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    step_states: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""
    triggered_by: str = ""
    error: Optional[str] = None
    parent_workflow_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "def_name": self.def_name,
            "status": self.status.value,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "step_states": self.step_states,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "triggered_by": self.triggered_by,
            "error": self.error,
            "parent_workflow_id": self.parent_workflow_id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Workflow":
        return cls(
            id=d["id"],
            def_name=d.get("def_name", ""),
            status=WorkflowStatus(d.get("status", "pending")),
            inputs=d.get("inputs", {}) or {},
            outputs=d.get("outputs", {}) or {},
            step_states=d.get("step_states", {}) or {},
            created_at=d.get("created_at", ""),
            started_at=d.get("started_at", ""),
            completed_at=d.get("completed_at", ""),
            triggered_by=d.get("triggered_by", ""),
            error=d.get("error"),
            parent_workflow_id=d.get("parent_workflow_id"),
            metadata=d.get("metadata", {}) or {},
        )


@dataclass
class WorkflowEvent:
    id: int
    workflow_id: str
    step_id: str
    event_type: str
    timestamp: str
    actor: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "step_id": self.step_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "actor": self.actor,
            "payload": self.payload,
        }


class _ConditionError(Exception):
    pass


class _WorkflowCancelled(Exception):
    pass


class HiveWorkflowEngine:
    """Persistent workflow orchestrator that uses the NexusHive message board.

    Design notes:
    * Transport layer: `NexusHiveBridge` (writes `task` and reads `task_done`).
    * State layer: same SQLite DB; tables `workflows`, `workflow_steps`, `workflow_events`.
    * Correlation: every dispatch writes `metadata.workflow_id / step_id / task_id`;
      a background poller consumes `task_done` rows whose `metadata.task_id`
      matches a dispatched step. Responses update step state, not the message
      board itself — that keeps the board as a clean transport.
    * Concurrency: a single in-process `ThreadPoolExecutor` for sync steps +
      an asyncio loop for the poller / waiters. The engine itself is sync;
      use `asyncio.to_thread` from async code.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY,
        def_name TEXT NOT NULL,
        status TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_workflows_status ON workflows(status, created_at);

    CREATE TABLE IF NOT EXISTS workflow_steps (
        workflow_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER DEFAULT 0,
        task_id INTEGER,
        dispatched_at TEXT,
        completed_at TEXT,
        result TEXT,
        PRIMARY KEY (workflow_id, step_id)
    );
    CREATE INDEX IF NOT EXISTS idx_wfsteps_status ON workflow_steps(workflow_id, status);
    CREATE INDEX IF NOT EXISTS idx_wfsteps_task ON workflow_steps(task_id);

    CREATE TABLE IF NOT EXISTS workflow_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id TEXT NOT NULL,
        step_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL,
        actor TEXT NOT NULL,
        payload TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_wfevents_wf ON workflow_events(workflow_id, id);
    """

    def __init__(
        self,
        agent_name: str,
        bridge: Optional[NexusHiveBridge] = None,
        db_path: Optional[Union[str, Path]] = None,
        poll_interval_s: float = 1.0,
        max_concurrent_steps: int = 4,
        executor: Optional[ThreadPoolExecutor] = None,
    ):
        self.agent_name = agent_name
        self.bridge = bridge or NexusHiveBridge(agent_name=agent_name, db_path=str(db_path) if db_path else None)
        self.db_path = Path(db_path) if db_path else self.bridge.db_path
        self.poll_interval_s = float(poll_interval_s)
        self.max_concurrent_steps = int(max_concurrent_steps)
        self._executor = executor or ThreadPoolExecutor(
            max_workers=max(2, max_concurrent_steps * 2),
            thread_name_prefix=f"hive-wf-{agent_name}",
        )
        self._builtin_fns: Dict[str, Callable[..., Any]] = {}
        self._hooks: Dict[str, List[Callable[..., None]]] = {
            "workflow_start": [],
            "workflow_complete": [],
            "workflow_fail": [],
            "step_start": [],
            "step_complete": [],
            "step_fail": [],
        }
        self._running: Dict[str, Future] = {}
        self._cancelled: Set[str] = set()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._init_schema()

    def _init_schema(self) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.executescript(self.SCHEMA)
        conn.commit()
        conn.close()

    def register_builtin(self, name: str, fn: Callable[..., Any]) -> None:
        self._builtin_fns[name] = fn

    def add_hook(self, event: str, fn: Callable[..., None]) -> None:
        self._hooks.setdefault(event, []).append(fn)

    def _emit_hook(self, event: str, **payload: Any) -> None:
        for fn in list(self._hooks.get(event, [])):
            try:
                fn(**payload)
            except Exception:
                logger.exception("hook %s raised", event)

    def start(
        self,
        def_name: str,
        inputs: Optional[Dict[str, Any]] = None,
        workflow_id: Optional[str] = None,
        triggered_by: Optional[str] = None,
        parent_workflow_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        defs: Optional[WorkflowDef] = None,
        wait: bool = False,
        timeout_s: Optional[float] = None,
    ) -> Workflow:
        if defs is None:
            raise ValueError("defs (WorkflowDef) is required; engine does not auto-load templates")
        if not def_name:
            def_name = defs.name
        errors = defs.validate()
        if errors:
            raise ValueError(f"invalid workflow definition: {'; '.join(errors)}")

        wf_id = workflow_id or self._new_workflow_id()
        wf = Workflow(
            id=wf_id,
            def_name=def_name,
            inputs=inputs or {},
            triggered_by=triggered_by or self.agent_name,
            parent_workflow_id=parent_workflow_id,
            metadata=metadata or {},
            created_at=_now_iso(),
        )
        for s in defs.steps:
            wf.step_states[s.id] = {
                "status": StepStatus.PENDING.value,
                "attempts": 0,
                "task_id": None,
                "dispatched_at": None,
                "completed_at": None,
                "result": None,
            }
        wf.status = WorkflowStatus.RUNNING
        wf.started_at = _now_iso()
        self._persist_workflow(wf)
        self._record_event(wf.id, "", "workflow_start", {"def": def_name, "triggered_by": wf.triggered_by})
        self._emit_hook("workflow_start", workflow=wf, defs=defs)

        if defs.metadata.get("sync") or wait:
            future = self._executor.submit(self._run, wf, defs)
        else:
            future = self._executor.submit(self._run, wf, defs)
        with self._lock:
            self._running[wf.id] = future

        if wait:
            try:
                future.result(timeout=timeout_s)
            except Exception as e:
                logger.warning("workflow %s wait ended with %s", wf.id, e)
        return wf

    def _run(self, wf: Workflow, defs: WorkflowDef) -> None:
        try:
            self._execute_dag(wf, defs)
        except _WorkflowCancelled:
            wf.status = WorkflowStatus.CANCELLED
            wf.completed_at = _now_iso()
            self._persist_workflow(wf)
            self._record_event(wf.id, "", "workflow_cancelled", {})
            self._emit_hook("workflow_fail", workflow=wf, reason="cancelled")
        except Exception as e:
            wf.status = WorkflowStatus.FAILED
            wf.error = str(e)
            wf.completed_at = _now_iso()
            self._persist_workflow(wf)
            self._record_event(wf.id, "", "workflow_error", {"error": str(e)})
            self._emit_hook("workflow_fail", workflow=wf, reason=str(e))
            logger.exception("workflow %s crashed", wf.id)
        finally:
            with self._lock:
                self._running.pop(wf.id, None)

    def _execute_dag(self, wf: Workflow, defs: WorkflowDef) -> None:
        index = defs.index()
        outputs: Dict[str, Any] = dict(wf.inputs)
        pending: Set[str] = {s.id for s in defs.steps}
        inflight: Dict[str, WorkflowStep] = {}
        deadline = time.monotonic() + defs.default_timeout_s

        def _cancel_check() -> None:
            if wf.id in self._cancelled:
                raise _WorkflowCancelled(wf.id)

        while pending or inflight:
            _cancel_check()
            if time.monotonic() > deadline:
                raise TimeoutError(f"workflow {wf.id} exceeded default_timeout_s={defs.default_timeout_s}")
            for sid in list(pending):
                step = index.get(sid)
                if step is None:
                    pending.discard(sid)
                    continue
                if not self._deps_satisfied(step, outputs, defs):
                    continue
                if not self._condition_holds(step, outputs):
                    self._mark_skipped(wf, step, "condition_false")
                    pending.discard(sid)
                    continue
                if step.kind == StepKind.PARALLEL:
                    self._dispatch_parallel(wf, step, outputs)
                    self._mark_completed(wf, step, StepResult(status=StepStatus.COMPLETED, output=_collect_child_outputs(step, outputs)))
                    pending.discard(sid)
                    continue
                if len(inflight) >= self.max_concurrent_steps:
                    break
                self._dispatch(wf, step, outputs)
                inflight[step.id] = step
                pending.discard(sid)

            if not inflight:
                if not pending:
                    break
                time.sleep(self.poll_interval_s)
                continue

            step = next(iter(inflight.keys()))
            step_obj = inflight.pop(step)
            self._await_step(wf, step_obj, deadline, outputs, defs)
            step_state = wf.step_states[step_obj.id]
            st = step_state["status"]
            if st == StepStatus.COMPLETED.value:
                outputs[step_obj.id] = step_state.get("result", {}).get("output")
            elif st == StepStatus.SKIPPED.value:
                outputs[step_obj.id] = None
            else:
                self._handle_step_error(wf, step_obj, step_state, defs)
                if wf.status == WorkflowStatus.FAILED:
                    return

        wf.outputs = {sid: outputs.get(sid) for sid in {s.id for s in defs.steps}}
        wf.status = WorkflowStatus.COMPLETED
        wf.completed_at = _now_iso()
        self._persist_workflow(wf)
        self._record_event(wf.id, "", "workflow_complete", {"status": "ok"})
        self._emit_hook("workflow_complete", workflow=wf, defs=defs)

    def _deps_satisfied(self, step: WorkflowStep, outputs: Dict[str, Any], defs: WorkflowDef) -> bool:
        index = defs.index()
        for dep in step.depends_on:
            parent = index.get(dep)
            if parent is None:
                return False
            state = self._workflow_state_for_step(parent.id, outputs)
            if state != "ok":
                return False
        return True

    def _workflow_state_for_step(self, step_id: str, outputs: Dict[str, Any]) -> str:
        val = outputs.get(step_id)
        if val is None and step_id not in outputs:
            return "missing"
        if isinstance(val, _Marker) and val.kind == "skipped":
            return "skip"
        return "ok"

    def _condition_holds(self, step: WorkflowStep, outputs: Dict[str, Any]) -> bool:
        if step.kind != StepKind.CONDITION:
            return True
        if not step.condition_expr:
            return True
        try:
            env = {k: v for k, v in outputs.items() if not isinstance(v, _Marker)}
            env["__outputs__"] = {k: v for k, v in outputs.items() if not isinstance(v, _Marker)}
            env["__inputs__"] = dict(getattr(self, "_current_inputs", {}) or {})
            env["__builtins__"] = _SAFE_BUILTINS
            return bool(eval(step.condition_expr, env, env))
        except Exception as e:
            raise _ConditionError(f"condition '{step.condition_expr}' failed: {e}") from e

    def _dispatch(self, wf: Workflow, step: WorkflowStep, outputs: Dict[str, Any]) -> None:
        self._emit_hook("step_start", workflow=wf, step=step)
        self._record_event(wf.id, step.id, "step_start", {"kind": step.kind.value, "agent": step.target_agent})
        state = wf.step_states[step.id]
        state["status"] = StepStatus.DISPATCHED.value
        state["attempts"] = int(state.get("attempts", 0) or 0) + 1
        state["dispatched_at"] = _now_iso()

        if step.kind == StepKind.AGENT:
            prompt = _render_template(step.prompt, outputs, wf.inputs)
            meta = {
                "workflow_id": wf.id,
                "step_id": step.id,
                "step_attempt": state["attempts"],
            }
            self.bridge.send_message(
                target=step.target_agent or "*",
                content=prompt,
                msg_type="task",
                channel=step.channel,
                metadata=meta,
            )
            state["task_id"] = self._latest_msg_id()
        elif step.kind == StepKind.BUILTIN:
            self._executor.submit(self._run_builtin, wf, step, dict(outputs))
            return
        elif step.kind == StepKind.WAIT:
            state["status"] = StepStatus.WAITING.value
            self._persist_workflow(wf)
            return
        elif step.kind == StepKind.HUMAN:
            self.bridge.send_message(
                target="*",
                content=step.prompt or f"[HUMAN-APPROVAL-REQUIRED] workflow={wf.id} step={step.id}",
                msg_type="human_request",
                channel=step.channel,
                metadata={"workflow_id": wf.id, "step_id": step.id, "approval": True},
            )
            state["status"] = StepStatus.WAITING.value
            self._persist_workflow(wf)
            return
        else:
            state["status"] = StepStatus.COMPLETED.value
            state["result"] = StepResult(status=StepStatus.COMPLETED).to_dict()
            state["completed_at"] = _now_iso()
            self._persist_workflow(wf)
            return

        self._persist_workflow(wf)

    def _dispatch_parallel(self, wf: Workflow, parent: WorkflowStep, outputs: Dict[str, Any]) -> None:
        for child in parent.parallel_children:
            if child.id in wf.step_states:
                continue
            wf.step_states[child.id] = {
                "status": StepStatus.PENDING.value,
                "attempts": 0,
                "task_id": None,
                "dispatched_at": None,
                "completed_at": None,
                "result": None,
            }
        self._persist_workflow(wf)
        futures = []
        for child in parent.parallel_children:
            self._emit_hook("step_start", workflow=wf, step=child)
            self._record_event(wf.id, child.id, "step_start", {"kind": child.kind.value, "parallel_parent": parent.id})
            self._dispatch(wf, child, outputs)
            futures.append((child, self._executor.submit(self._await_step, wf, child, time.monotonic() + child.timeout_s, outputs, None)))
        for child, fut in futures:
            try:
                fut.result(timeout=child.timeout_s + 5.0)
            except Exception as e:
                state = wf.step_states[child.id]
                if state["status"] not in (StepStatus.COMPLETED.value, StepStatus.SKIPPED.value):
                    state["status"] = StepStatus.FAILED.value
                    state["result"] = StepResult(status=StepStatus.FAILED, error=str(e)).to_dict()
                    self._persist_workflow(wf)
        self._record_event(wf.id, parent.id, "parallel_complete", {"children": [c.id for c in parent.parallel_children]})

    def _run_builtin(self, wf: Workflow, step: WorkflowStep, outputs: Dict[str, Any]) -> None:
        fn = self._builtin_fns.get(step.builtin_fn or "")
        state = wf.step_states[step.id]
        try:
            if fn is None:
                raise RuntimeError(f"builtin '{step.builtin_fn}' not registered")
            out = fn(wf=wf, step=step, outputs=outputs, args=step.builtin_args)
            state["status"] = StepStatus.COMPLETED.value
            state["result"] = StepResult(status=StepStatus.COMPLETED, output=out).to_dict()
        except Exception as e:
            state["status"] = StepStatus.FAILED.value
            state["result"] = StepResult(status=StepStatus.FAILED, error=str(e)).to_dict()
            self._emit_hook("step_fail", workflow=wf, step=step, error=str(e))
        finally:
            state["completed_at"] = _now_iso()
            self._persist_workflow(wf)
            self._record_event(wf.id, step.id, "step_builtin_done", {"status": state["status"]})

    def _await_step(
        self,
        wf: Workflow,
        step: WorkflowStep,
        deadline: float,
        outputs: Dict[str, Any],
        defs: Optional[WorkflowDef],
    ) -> None:
        if wf.id in self._cancelled:
            raise _WorkflowCancelled(wf.id)
        state = wf.step_states[step.id]
        if state["status"] in (
            StepStatus.COMPLETED.value,
            StepStatus.SKIPPED.value,
            StepStatus.FAILED.value,
            StepStatus.CANCELLED.value,
            StepStatus.TIMEOUT.value,
        ):
            return

        if step.kind == StepKind.WAIT:
            self._poll_wait(wf, step, deadline)
            return

        if step.kind == StepKind.HUMAN:
            self._poll_human(wf, step, deadline)
            return

        task_id = state.get("task_id")
        if task_id is None:
            state["status"] = StepStatus.FAILED.value
            state["result"] = StepResult(status=StepStatus.FAILED, error="missing task_id").to_dict()
            state["completed_at"] = _now_iso()
            self._persist_workflow(wf)
            return

        step_deadline = min(deadline, time.monotonic() + step.timeout_s)
        attempt = state["attempts"]
        start_ts = time.monotonic()
        while True:
            if wf.id in self._cancelled:
                raise _WorkflowCancelled(wf.id)
            if time.monotonic() > step_deadline:
                state["status"] = StepStatus.TIMEOUT.value
                state["result"] = StepResult(
                    status=StepStatus.TIMEOUT,
                    error=f"step timed out after {step.timeout_s}s",
                ).to_dict()
                state["completed_at"] = _now_iso()
                self._persist_workflow(wf)
                self._record_event(wf.id, step.id, "step_timeout", {"attempt": attempt})
                return
            response = self._fetch_response(task_id, step.target_agent or "")
            if response is not None:
                self._apply_response(wf, step, response, attempt, start_ts)
                return
            time.sleep(self.poll_interval_s)

    def _poll_wait(self, wf: Workflow, step: WorkflowStep, deadline: float) -> None:
        predicate = step.wait_predicate or "True"
        while True:
            if wf.id in self._cancelled:
                raise _WorkflowCancelled(wf.id)
            if time.monotonic() > deadline:
                state = wf.step_states[step.id]
                state["status"] = StepStatus.TIMEOUT.value
                state["result"] = StepResult(status=StepStatus.TIMEOUT, error="wait deadline").to_dict()
                state["completed_at"] = _now_iso()
                self._persist_workflow(wf)
                return
            try:
                env = {"messages": self._recent_messages(50), "__builtins__": _SAFE_BUILTINS}
                if eval(predicate, env, env):
                    state = wf.step_states[step.id]
                    state["status"] = StepStatus.COMPLETED.value
                    state["result"] = StepResult(status=StepStatus.COMPLETED, output=env["messages"][:1] if env["messages"] else None).to_dict()
                    state["completed_at"] = _now_iso()
                    self._persist_workflow(wf)
                    self._record_event(wf.id, step.id, "wait_satisfied", {})
                    return
            except Exception as e:
                state = wf.step_states[step.id]
                state["status"] = StepStatus.FAILED.value
                state["result"] = StepResult(status=StepStatus.FAILED, error=f"wait predicate error: {e}").to_dict()
                state["completed_at"] = _now_iso()
                self._persist_workflow(wf)
                return
            time.sleep(self.poll_interval_s)

    def _poll_human(self, wf: Workflow, step: WorkflowStep, deadline: float) -> None:
        step_deadline = time.monotonic() + step.timeout_s
        while True:
            if wf.id in self._cancelled:
                raise _WorkflowCancelled(wf.id)
            if time.monotonic() > step_deadline:
                state = wf.step_states[step.id]
                state["status"] = StepStatus.TIMEOUT.value
                state["result"] = StepResult(status=StepStatus.TIMEOUT, error="human approval timeout").to_dict()
                state["completed_at"] = _now_iso()
                self._persist_workflow(wf)
                return
            decision = self._find_human_decision(wf.id, step.id)
            if decision is not None:
                state = wf.step_states[step.id]
                approved = bool(decision.get("approved"))
                state["status"] = StepStatus.COMPLETED.value if approved else StepStatus.FAILED.value
                state["result"] = StepResult(
                    status=StepStatus.COMPLETED if approved else StepStatus.FAILED,
                    output=decision,
                    error=None if approved else "human rejected",
                ).to_dict()
                state["completed_at"] = _now_iso()
                self._persist_workflow(wf)
                self._record_event(wf.id, step.id, "human_decision", decision)
                return
            time.sleep(self.poll_interval_s)

    def _apply_response(self, wf: Workflow, step: WorkflowStep, response: Dict[str, Any], attempt: int, start_ts: float) -> None:
        state = wf.step_states[step.id]
        meta = {}
        try:
            meta = json.loads(response.get("metadata") or "{}")
        except Exception:
            pass
        content = response.get("content", "")
        success = self._is_success_response(meta, content)
        status = StepStatus.COMPLETED if success else StepStatus.FAILED
        result = StepResult(
            status=status,
            output=self._extract_output(meta, content),
            error=None if success else f"agent reported failure: {content[:200]}",
            raw_content=content,
            response_metadata=meta,
            duration_ms=int((time.monotonic() - start_ts) * 1000),
            attempts=attempt,
        )
        state["status"] = status.value
        state["result"] = result.to_dict()
        state["completed_at"] = _now_iso()
        self._persist_workflow(wf)
        self._record_event(
            wf.id,
            step.id,
            "step_response",
            {"task_id": state.get("task_id"), "status": status.value, "agent": response.get("sender")},
        )
        if success:
            self._emit_hook("step_complete", workflow=wf, step=step, result=result)
        else:
            self._emit_hook("step_fail", workflow=wf, step=step, error=result.error)

    def _is_success_response(self, meta: Dict[str, Any], content: str) -> bool:
        if isinstance(meta, dict):
            if "success" in meta:
                return bool(meta["success"])
            if meta.get("status"):
                return str(meta["status"]).lower() in ("ok", "success", "completed", "done")
        low = (content or "").strip().lower()
        if low.startswith("[auto]"):
            return True
        if low.startswith("error") or low.startswith("fail"):
            return False
        return True

    def _extract_output(self, meta: Dict[str, Any], content: str) -> Any:
        if isinstance(meta, dict):
            for key in ("output", "result", "data", "payload"):
                if key in meta:
                    return meta[key]
        return content

    def _handle_step_error(self, wf: Workflow, step: WorkflowStep, state: Dict[str, Any], defs: WorkflowDef) -> None:
        attempts = int(state.get("attempts", 0) or 0)
        on_error = step.on_error
        if on_error == "retry" and attempts < step.max_retries:
            state["status"] = StepStatus.PENDING.value
            state["result"] = None
            self._persist_workflow(wf)
            self._record_event(wf.id, step.id, "step_retry", {"attempt": attempts + 1})
            time.sleep(step.retry_backoff_s * max(1, attempts))
            return
        if on_error == "skip":
            self._mark_skipped(wf, step, "on_error_skip")
            return
        if on_error == "continue":
            state["status"] = StepStatus.SKIPPED.value
            state["result"] = StepResult(status=StepStatus.SKIPPED, error=state.get("result", {}).get("error")).to_dict()
            state["completed_at"] = _now_iso()
            self._persist_workflow(wf)
            return
        wf.status = WorkflowStatus.FAILED
        wf.error = f"step {step.id} failed: {state.get('result', {}).get('error', 'unknown')}"
        wf.completed_at = _now_iso()
        self._persist_workflow(wf)
        self._record_event(wf.id, step.id, "workflow_failed_step", {"step": step.id})

    def _mark_skipped(self, wf: Workflow, step: WorkflowStep, reason: str) -> None:
        state = wf.step_states[step.id]
        state["status"] = StepStatus.SKIPPED.value
        state["result"] = StepResult(status=StepStatus.SKIPPED, error=reason).to_dict()
        state["completed_at"] = _now_iso()
        self._persist_workflow(wf)
        self._record_event(wf.id, step.id, "step_skipped", {"reason": reason})

    def _mark_completed(self, wf: Workflow, step: WorkflowStep, result: StepResult) -> None:
        state = wf.step_states[step.id]
        state["status"] = result.status.value
        state["result"] = result.to_dict()
        state["completed_at"] = _now_iso()
        self._persist_workflow(wf)
        self._record_event(wf.id, step.id, "step_complete", {"status": result.status.value})
        self._emit_hook("step_complete", workflow=wf, step=step, result=result)

    def _fetch_response(self, task_id: int, agent: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id, sender, content, metadata, timestamp FROM messages
               WHERE id > ? AND msg_type IN ('task_done', 'response')
                 AND (sender = ? OR ? = '')
               ORDER BY id ASC LIMIT 1""",
            (task_id, agent, agent),
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except Exception:
            d["metadata"] = {}
        return d

    def _find_human_decision(self, workflow_id: str, step_id: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT id, sender, content, metadata, timestamp FROM messages
               WHERE msg_type = 'human_decision' AND id > COALESCE(
                   (SELECT MAX(id) FROM messages
                    WHERE msg_type = 'human_request' AND json_extract(metadata, '$.workflow_id') = ?
                      AND json_extract(metadata, '$.step_id') = ?), 0)
                 AND json_extract(metadata, '$.workflow_id') = ?
                 AND json_extract(metadata, '$.step_id') = ?
               ORDER BY id ASC LIMIT 1""",
            (workflow_id, step_id, workflow_id, step_id),
        ).fetchone()
        conn.close()
        if not row:
            return None
        d = dict(row)
        try:
            d["metadata"] = json.loads(d.get("metadata") or "{}")
        except Exception:
            d["metadata"] = {}
        return d

    def _recent_messages(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.bridge.get_all_messages(limit=limit)

    def _latest_msg_id(self) -> int:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        row = conn.execute("SELECT MAX(id) FROM messages").fetchone()
        conn.close()
        return int(row[0] or 0)

    def cancel(self, workflow_id: str) -> bool:
        with self._lock:
            if workflow_id in self._running:
                self._cancelled.add(workflow_id)
                return True
        return False

    def wait(self, workflow_id: str, timeout_s: Optional[float] = None) -> Workflow:
        deadline = time.monotonic() + (timeout_s or 1e9)
        while time.monotonic() < deadline:
            wf = self.get(workflow_id)
            if wf is None:
                raise KeyError(workflow_id)
            if wf.status in (
                WorkflowStatus.COMPLETED,
                WorkflowStatus.FAILED,
                WorkflowStatus.CANCELLED,
            ):
                return wf
            time.sleep(self.poll_interval_s)
        raise TimeoutError(f"workflow {workflow_id} did not finish in {timeout_s}s")

    async def await_completion(self, workflow_id: str, timeout_s: Optional[float] = None) -> Workflow:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.wait, workflow_id, timeout_s)

    def resume(self, workflow_id: str, defs: WorkflowDef) -> Workflow:
        wf = self.get(workflow_id)
        if wf is None:
            raise KeyError(workflow_id)
        if wf.status not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING, WorkflowStatus.PARTIAL):
            return wf
        wf.status = WorkflowStatus.RUNNING
        wf.started_at = wf.started_at or _now_iso()
        self._persist_workflow(wf)
        self._record_event(wf.id, "", "workflow_resume", {})
        future = self._executor.submit(self._run, wf, defs)
        with self._lock:
            self._running[wf.id] = future
        return wf

    def get(self, workflow_id: str) -> Optional[Workflow]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        row = conn.execute("SELECT payload FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        conn.close()
        if not row:
            return None
        try:
            return Workflow.from_dict(json.loads(row[0]))
        except Exception:
            return None

    def list_runs(
        self,
        status: Optional[WorkflowStatus] = None,
        def_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[Workflow]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        sql = "SELECT payload FROM workflows"
        args: List[Any] = []
        where = []
        if status:
            where.append("status = ?")
            args.append(status.value)
        if def_name:
            where.append("def_name = ?")
            args.append(def_name)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        conn.close()
        out: List[Workflow] = []
        for r in rows:
            try:
                out.append(Workflow.from_dict(json.loads(r["payload"])))
            except Exception:
                continue
        return out

    def get_events(self, workflow_id: str, limit: int = 200) -> List[WorkflowEvent]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, workflow_id, step_id, event_type, timestamp, actor, payload FROM workflow_events WHERE workflow_id = ? ORDER BY id ASC LIMIT ?",
            (workflow_id, limit),
        ).fetchall()
        conn.close()
        out: List[WorkflowEvent] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {}
            out.append(WorkflowEvent(
                id=r["id"],
                workflow_id=r["workflow_id"],
                step_id=r["step_id"],
                event_type=r["event_type"],
                timestamp=r["timestamp"],
                actor=r["actor"],
                payload=payload,
            ))
        return out

    def _persist_workflow(self, wf: Workflow) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """INSERT INTO workflows (id, def_name, status, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, payload=excluded.payload, updated_at=excluded.updated_at""",
                (wf.id, wf.def_name, wf.status.value, json.dumps(wf.to_dict()), wf.created_at or _now_iso(), _now_iso()),
            )
            for sid, st in wf.step_states.items():
                conn.execute(
                    """INSERT INTO workflow_steps (workflow_id, step_id, status, attempts, task_id, dispatched_at, completed_at, result)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(workflow_id, step_id) DO UPDATE SET
                         status=excluded.status, attempts=excluded.attempts, task_id=excluded.task_id,
                         dispatched_at=excluded.dispatched_at, completed_at=excluded.completed_at, result=excluded.result""",
                    (
                        wf.id,
                        sid,
                        st.get("status", StepStatus.PENDING.value),
                        int(st.get("attempts", 0) or 0),
                        st.get("task_id"),
                        st.get("dispatched_at"),
                        st.get("completed_at"),
                        json.dumps(st.get("result")) if st.get("result") is not None else None,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _record_event(self, workflow_id: str, step_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.execute(
                "INSERT INTO workflow_events (workflow_id, step_id, event_type, timestamp, actor, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (workflow_id, step_id, event_type, _now_iso(), self.agent_name, json.dumps(payload or {})),
            )
            conn.commit()
        finally:
            conn.close()

    def start_watchdog(self, interval_s: float = 30.0) -> None:
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._stop_event.clear()

        def _run() -> None:
            while not self._stop_event.is_set():
                try:
                    self._reap_stalled(interval_s)
                except Exception:
                    logger.exception("watchdog iteration failed")
                self._stop_event.wait(interval_s)

        self._watchdog_thread = threading.Thread(target=_run, name=f"hive-wf-watchdog-{self.agent_name}", daemon=True)
        self._watchdog_thread.start()

    def stop_watchdog(self) -> None:
        self._stop_event.set()
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=2.0)

    def _reap_stalled(self, threshold_s: float) -> None:
        cutoff = time.time() - threshold_s
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, def_name, payload FROM workflows WHERE status = ?",
            (WorkflowStatus.RUNNING.value,),
        ).fetchall()
        conn.close()
        for r in rows:
            try:
                wf = Workflow.from_dict(json.loads(r["payload"]))
            except Exception:
                continue
            last_seen = wf.completed_at or wf.started_at or wf.created_at
            try:
                ts = datetime.fromisoformat(last_seen).timestamp()
            except Exception:
                continue
            if ts < cutoff and wf.id not in self._running:
                wf.status = WorkflowStatus.FAILED
                wf.error = f"stalled: no progress for {threshold_s}s"
                wf.completed_at = _now_iso()
                self._persist_workflow(wf)
                self._record_event(wf.id, "", "workflow_stalled", {"threshold_s": threshold_s})

    def _new_workflow_id(self) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"wf_{self.agent_name}_{ts}_{uuid.uuid4().hex[:8]}"

    def shutdown(self, wait: bool = True) -> None:
        self.stop_watchdog()
        with self._lock:
            futures = list(self._running.values())
        for f in futures:
            if wait:
                try:
                    f.result(timeout=2.0)
                except Exception:
                    pass
            else:
                f.cancel()
        self._executor.shutdown(wait=wait)


class WorkflowRegistry:
    """In-memory registry of reusable `WorkflowDef` templates."""

    def __init__(self, engine: Optional[HiveWorkflowEngine] = None):
        self.engine = engine
        self._defs: Dict[str, WorkflowDef] = {}

    def register(self, wdef: WorkflowDef) -> None:
        errors = wdef.validate()
        if errors:
            raise ValueError(f"invalid workflow '{wdef.name}': {'; '.join(errors)}")
        self._defs[wdef.name] = wdef

    def get(self, name: str) -> WorkflowDef:
        if name not in self._defs:
            raise KeyError(f"workflow template not found: {name}")
        return self._defs[name]

    def list(self) -> List[str]:
        return sorted(self._defs.keys())

    def unregister(self, name: str) -> bool:
        return self._defs.pop(name, None) is not None


class WorkflowTemplate:
    """Fluent builder for `WorkflowDef` — keeps definitions readable in code."""

    def __init__(self, name: str, description: str = "", version: str = "1.0"):
        self._def = WorkflowDef(name=name, description=description, version=version)
        self._current_parent: Optional[WorkflowStep] = None

    def tag(self, *tags: str) -> "WorkflowTemplate":
        self._def.tags.extend(tags)
        return self

    def metadata(self, **kv: Any) -> "WorkflowTemplate":
        self._def.metadata.update(kv)
        return self

    def timeout(self, seconds: float) -> "WorkflowTemplate":
        self._def.default_timeout_s = float(seconds)
        return self

    def step(
        self,
        step_id: str,
        title: str = "",
        description: str = "",
        target: Optional[str] = None,
        prompt: str = "",
        depends_on: Optional[List[str]] = None,
        timeout_s: float = 300.0,
        retries: int = 1,
        on_error: str = "fail",
        channel: str = "general",
        **meta: Any,
    ) -> "WorkflowTemplate":
        step = WorkflowStep(
            id=step_id,
            kind=StepKind.AGENT,
            title=title or step_id,
            description=description,
            target_agent=target,
            prompt=prompt,
            depends_on=list(depends_on or []),
            timeout_s=timeout_s,
            max_retries=retries,
            on_error=on_error,
            channel=channel,
            metadata=dict(meta),
        )
        self._def.steps.append(step)
        self._current_parent = step
        return self

    def condition(
        self,
        step_id: str,
        expr: str,
        depends_on: Optional[List[str]] = None,
        on_error: str = "fail",
    ) -> "WorkflowTemplate":
        step = WorkflowStep(
            id=step_id,
            kind=StepKind.CONDITION,
            depends_on=list(depends_on or []),
            condition_expr=expr,
            on_error=on_error,
        )
        self._def.steps.append(step)
        self._current_parent = step
        return self

    def builtin(
        self,
        step_id: str,
        fn_name: str,
        args: Optional[Dict[str, Any]] = None,
        depends_on: Optional[List[str]] = None,
        timeout_s: float = 60.0,
        on_error: str = "fail",
    ) -> "WorkflowTemplate":
        step = WorkflowStep(
            id=step_id,
            kind=StepKind.BUILTIN,
            depends_on=list(depends_on or []),
            builtin_fn=fn_name,
            builtin_args=dict(args or {}),
            timeout_s=timeout_s,
            on_error=on_error,
        )
        self._def.steps.append(step)
        self._current_parent = step
        return self

    def wait(
        self,
        step_id: str,
        predicate: str,
        depends_on: Optional[List[str]] = None,
        timeout_s: float = 3600.0,
        on_error: str = "fail",
    ) -> "WorkflowTemplate":
        step = WorkflowStep(
            id=step_id,
            kind=StepKind.WAIT,
            depends_on=list(depends_on or []),
            wait_predicate=predicate,
            timeout_s=timeout_s,
            on_error=on_error,
        )
        self._def.steps.append(step)
        self._current_parent = step
        return self

    def human(
        self,
        step_id: str,
        prompt: str,
        depends_on: Optional[List[str]] = None,
        timeout_s: float = 86400.0,
        channel: str = "general",
        on_error: str = "fail",
    ) -> "WorkflowTemplate":
        step = WorkflowStep(
            id=step_id,
            kind=StepKind.HUMAN,
            prompt=prompt,
            depends_on=list(depends_on or []),
            timeout_s=timeout_s,
            channel=channel,
            on_error=on_error,
        )
        self._def.steps.append(step)
        self._current_parent = step
        return self

    def parallel(self, parent_id: str, *children: WorkflowStep) -> "WorkflowTemplate":
        parent = WorkflowStep(id=parent_id, kind=StepKind.PARALLEL, parallel_children=list(children))
        self._def.steps.append(parent)
        self._current_parent = parent
        return self

    def build(self) -> WorkflowDef:
        return self._def


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SAFE_BUILTINS: Dict[str, Any] = {
    "len": len, "bool": bool, "int": int, "float": float, "str": str,
    "min": min, "max": max, "sum": sum, "abs": abs, "round": round,
    "any": any, "all": all, "list": list, "dict": dict, "tuple": tuple,
    "set": set, "isinstance": isinstance, "type": type, "repr": repr,
    "enumerate": enumerate, "zip": zip, "map": map, "filter": filter,
    "range": range, "sorted": sorted, "reversed": reversed,
    "True": True, "False": False, "None": None,
}


def _render_template(template: str, outputs: Dict[str, Any], inputs: Dict[str, Any]) -> str:
    if not template:
        return ""
    env = {k: v for k, v in outputs.items() if not isinstance(v, _Marker)}
    env["__inputs__"] = dict(inputs or {})
    env["__outputs__"] = {k: v for k, v in env.items() if k != "__inputs__"}
    env["__builtins__"] = _SAFE_BUILTINS
    try:
        return str(eval(f"f'''{_escape_py(template)}'''", env, env))
    except Exception:
        return template


def _escape_py(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _collect_child_outputs(parent: WorkflowStep, outputs: Dict[str, Any]) -> Dict[str, Any]:
    return {c.id: outputs.get(c.id) for c in parent.parallel_children}


class _Marker:
    def __init__(self, kind: str, value: Any = None):
        self.kind = kind
        self.value = value


def cli_status(limit: int = 20, db_path: Optional[str] = None) -> None:
    """Tiny CLI helper: print recent workflow runs."""
    eng = HiveWorkflowEngine(agent_name="cli", db_path=db_path) if db_path else HiveWorkflowEngine(agent_name="cli")
    runs = eng.list_runs(limit=limit)
    if not runs:
        print("[hive-workflow] no workflows found")
        return
    print(f"{'id':<48} {'def':<24} {'status':<10} {'started':<22} {'error'}")
    print("-" * 120)
    for w in runs:
        print(f"{w.id:<48} {w.def_name[:24]:<24} {w.status.value:<10} {(w.started_at or '-')[:22]:<22} {(w.error or '')[:40]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Hive Workflow Engine CLI")
    sub = parser.add_subparsers(dest="cmd")
    p_status = sub.add_parser("status", help="list recent workflows")
    p_status.add_argument("--limit", type=int, default=20)
    p_status.add_argument("--db", default=None)
    args = parser.parse_args()
    if args.cmd == "status":
        cli_status(limit=args.limit, db_path=args.db)
    else:
        parser.print_help()
