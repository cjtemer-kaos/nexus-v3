"""
Tool Call Guardrail Controller — F22

Detects and prevents tool call loops: repeated failures, no-progress reads,
and stuck tool patterns. Returns decisions (allow/warn/block/halt) that
the runtime can enforce.

Adapted for SuperNEXUS v2: simplified, integrated with tool_monitor
"""

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

logger = logging.getLogger("nexus-guardrails")

IDEMPOTENT_TOOLS = frozenset({
    "read_file", "search_files", "web_search", "web_extract",
    "browser_snapshot", "browser_console", "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file", "mcp_filesystem_list_directory",
    "mcp_filesystem_get_file_info", "mcp_filesystem_search_files",
})

MUTATING_TOOLS = frozenset({
    "terminal", "execute_code", "write_file", "patch",
    "browser_click", "browser_type", "browser_press", "browser_navigate",
    "send_message", "delegate_task",
})


@dataclass(frozen=True)
class GuardrailConfig:
    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset = field(default_factory=lambda: IDEMPOTENT_TOOLS)
    mutating_tools: frozenset = field(default_factory=lambda: MUTATING_TOOLS)


@dataclass(frozen=True)
class ToolCallSignature:
    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> "ToolCallSignature":
        canonical = json.dumps(args or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return cls(tool_name=tool_name, args_hash=_sha256(canonical))

    def to_metadata(self) -> dict:
        return {"tool_name": self.tool_name, "args_hash": self.args_hash}


@dataclass(frozen=True)
class GuardrailDecision:
    action: str = "allow"  # allow | warn | block | halt
    code: str = "allow"
    message: str = ""
    tool_name: str = ""
    count: int = 0
    signature: Optional[ToolCallSignature] = None

    @property
    def allows_execution(self) -> bool:
        return self.action in {"allow", "warn"}

    @property
    def should_halt(self) -> bool:
        return self.action in {"block", "halt"}

    def to_metadata(self) -> dict:
        data = {"action": self.action, "code": self.code, "message": self.message,
                "tool_name": self.tool_name, "count": self.count}
        if self.signature:
            data["signature"] = self.signature.to_metadata()
        return data


class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls"""

    def __init__(self, config: Optional[GuardrailConfig] = None):
        self.config = config or GuardrailConfig()
        self._lock = threading.Lock()
        self.reset_for_turn()

    def reset_for_turn(self):
        self._exact_failure_counts = {}
        self._same_tool_failure_counts = {}
        self._no_progress = {}
        self._halt_decision = None

    @property
    def halt_decision(self):
        return self._halt_decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> GuardrailDecision:
        signature = ToolCallSignature.from_call(tool_name, args)
        if not self.config.hard_stop_enabled:
            return GuardrailDecision(tool_name=tool_name, signature=signature)

        exact_count = self._exact_failure_counts.get(signature, 0)
        if exact_count >= self.config.exact_failure_block_after:
            decision = GuardrailDecision(
                action="block", code="repeated_exact_failure_block",
                message=f"Blocked {tool_name}: same call failed {exact_count} times with identical args. Change strategy.",
                tool_name=tool_name, count=exact_count, signature=signature,
            )
            self._halt_decision = decision
            return decision

        if tool_name in self.config.idempotent_tools and tool_name not in self.config.mutating_tools:
            record = self._no_progress.get(signature)
            if record and record[1] >= self.config.no_progress_block_after:
                decision = GuardrailDecision(
                    action="block", code="idempotent_no_progress_block",
                    message=f"Blocked {tool_name}: same result {record[1]} times. Use existing result or change query.",
                    tool_name=tool_name, count=record[1], signature=signature,
                )
                self._halt_decision = decision
                return decision

        return GuardrailDecision(tool_name=tool_name, signature=signature)

    def after_call(self, tool_name: str, args: Mapping[str, Any] | None,
                   result: str | None, *, failed: bool | None = None) -> GuardrailDecision:
        args = args if isinstance(args, Mapping) else {}
        signature = ToolCallSignature.from_call(tool_name, args)

        if failed is None:
            failed = self._classify_failure(tool_name, result)

        if failed:
            with self._lock:
                exact_count = self._exact_failure_counts.get(signature, 0) + 1
                self._exact_failure_counts[signature] = exact_count
                self._no_progress.pop(signature, None)

                same_count = self._same_tool_failure_counts.get(tool_name, 0) + 1
                self._same_tool_failure_counts[tool_name] = same_count

                if self.config.hard_stop_enabled and same_count >= self.config.same_tool_failure_halt_after:
                    decision = GuardrailDecision(
                        action="halt", code="same_tool_failure_halt",
                        message=f"Stopped {tool_name}: failed {same_count} times. Change approach.",
                        tool_name=tool_name, count=same_count, signature=signature,
                    )
                    self._halt_decision = decision
                    return decision

                if self.config.warnings_enabled and exact_count >= self.config.exact_failure_warn_after:
                    return GuardrailDecision(
                        action="warn", code="repeated_exact_failure_warning",
                        message=f"{tool_name} failed {exact_count} times with same args. Change strategy.",
                        tool_name=tool_name, count=exact_count, signature=signature,
                    )

            if self.config.warnings_enabled and same_count >= self.config.same_tool_failure_warn_after:
                return GuardrailDecision(
                    action="warn", code="same_tool_failure_warning",
                    message=f"{tool_name} failed {same_count} times. Change approach.",
                    tool_name=tool_name, count=same_count, signature=signature,
                )

            return GuardrailDecision(tool_name=tool_name, count=exact_count, signature=signature)

        # Success: reset failure counts
        self._exact_failure_counts.pop(signature, None)
        self._same_tool_failure_counts.pop(tool_name, None)

        # Track no-progress for idempotent tools
        if tool_name in self.config.idempotent_tools and tool_name not in self.config.mutating_tools:
            result_hash = _result_hash(result)
            previous = self._no_progress.get(signature)
            repeat_count = 1
            if previous and previous[0] == result_hash:
                repeat_count = previous[1] + 1
            self._no_progress[signature] = (result_hash, repeat_count)

            if self.config.warnings_enabled and repeat_count >= self.config.no_progress_warn_after:
                return GuardrailDecision(
                    action="warn", code="idempotent_no_progress_warning",
                    message=f"{tool_name} returned same result {repeat_count} times. Use existing result.",
                    tool_name=tool_name, count=repeat_count, signature=signature,
                )

        return GuardrailDecision(tool_name=tool_name, signature=signature)

    def _classify_failure(self, tool_name: str, result: str | None) -> bool:
        if not result:
            return False
        lower = result[:500].lower()
        if '"error"' in lower or '"failed"' in lower or result.startswith("Error"):
            return True
        if tool_name == "terminal":
            try:
                data = json.loads(result)
                if isinstance(data, dict) and data.get("exit_code", 0) != 0:
                    return True
            except (json.JSONDecodeError, TypeError):
                pass
        return False

    def get_stats(self) -> dict:
        return {
            "warnings_enabled": self.config.warnings_enabled,
            "hard_stop_enabled": self.config.hard_stop_enabled,
            "tracked_failures": len(self._exact_failure_counts),
            "tracked_no_progress": len(self._no_progress),
            "halt_triggered": self._halt_decision is not None,
        }


def synthetic_result(decision: GuardrailDecision) -> str:
    return json.dumps({"error": decision.message, "guardrail": decision.to_metadata()}, ensure_ascii=False)


def append_guidance(result: str, decision: GuardrailDecision) -> str:
    if decision.action not in {"warn", "halt", "block"} or not decision.message:
        return result
    label = "Tool loop hard stop" if decision.action in {"halt", "block"} else "Tool loop warning"
    suffix = f"\n\n[{label}: {decision.code}; count={decision.count}; {decision.message}]"
    return (result or "") + suffix


def _result_hash(result: str | None) -> str:
    try:
        parsed = json.loads(result or "")
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    except (json.JSONDecodeError, TypeError):
        canonical = result or ""
    return _sha256(canonical)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
