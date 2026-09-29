"""
Tool Guardrails - Loop detection and safety controls.

Adapted for SuperNEXUS v2.0

Provides:
- Detection of tool call loops
- Classification of idempotent vs mutating tools
- Blacklist for dangerous commands
- SSRF protection for web fetch
"""

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, FrozenSet, List, Optional

logger = logging.getLogger(__name__)


IDEMPOTENT_TOOL_NAMES: FrozenSet[str] = frozenset({
    "read_file", "glob_files", "grep_content", "search_files",
    "web_search", "web_fetch", "list_directory", "get_file_info",
    "session_search", "memory_search", "knowledge_search",
    "get_status", "health_check", "get_config",
    "lsp_diagnostics", "lsp_symbols",
})

MUTATING_TOOL_NAMES: FrozenSet[str] = frozenset({
    "terminal", "execute_command", "execute_code", "run_command",
    "write_file", "edit_file", "patch_file", "create_file",
    "delete_file", "remove_file", "move_file", "copy_file",
    "git_commit", "git_push", "git_pull",
    "browser_click", "browser_type", "browser_press", "browser_scroll",
    "send_message", "post_message",
    "database_write", "database_delete",
    "api_request", "http_request",
})

DANGEROUS_COMMAND_PATTERNS: List[re.Pattern] = [
    re.compile(r'rm\s+-rf\s+[/\\]'),
    re.compile(r'del\s+/[sq]'),
    re.compile(r'format\s+[a-z]:', re.IGNORECASE),
    re.compile(r'shred\s+-'),
    re.compile(r'>\s*/dev/sd[a-z]'),
    re.compile(r'shutdown\s+', re.IGNORECASE),
    re.compile(r'reboot\s+', re.IGNORECASE),
    re.compile(r'curl\s+.*\b169\.254\.169\.254', re.IGNORECASE),
    re.compile(r'wget\s+.*\b169\.254\.169\.254', re.IGNORECASE),
    re.compile(r'nmap\s+-[sTU]\s+10\.'),
    re.compile(r'nmap\s+-[sTU]\s+172\.'),
    re.compile(r'nmap\s+-[sTU]\s+192\.168'),
    re.compile(r'sudo\s+su\s*'),
    re.compile(r'chmod\s+777\s+'),
    re.compile(r'chown\s+root:'),
    re.compile(r'kill\s+-9\s+-1'),
]

PRIVATE_IP_PATTERNS: List[re.Pattern] = [
    re.compile(r'^127\.\d+\.\d+\.\d+$'),
    re.compile(r'^10\.\d{1,3}\.\d{1,3}\.\d{1,3}$'),
    re.compile(r'^172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}$'),
    re.compile(r'^192\.168\.\d{1,3}\.\d{1,3}$'),
    re.compile(r'^169\.254\.\d{1,3}\.\d{1,3}$'),
]

BLOCKED_HOSTNAMES: FrozenSet[str] = frozenset({
    "localhost", "localhost.localdomain",
    "metadata.google.internal", "metadata",
})


@dataclass
class ToolCallGuardrailConfig:
    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: FrozenSet[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: FrozenSet[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)


@dataclass
class ToolCallRecord:
    tool_name: str
    tool_input_hash: str
    tool_result: str = ""
    timestamp: float = 0.0


class ToolGuardrails:
    """Safety controller for tool execution."""

    def __init__(self, config: ToolCallGuardrailConfig = None):
        self.config = config or ToolCallGuardrailConfig()
        self._tool_history: List[ToolCallRecord] = []
        self._last_tool_results: List[str] = []

    def check_command_safety(self, command: str) -> tuple[bool, str]:
        if not command:
            return True, ""
        for pattern in DANGEROUS_COMMAND_PATTERNS:
            if pattern.search(command):
                return False, f"Dangerous command detected: {pattern.pattern}"
        return True, ""

    def check_url_safety(self, url: str) -> tuple[bool, str]:
        if not url:
            return True, ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            hostname = parsed.hostname.lower() if parsed.hostname else ""
            if hostname in BLOCKED_HOSTNAMES:
                return False, f"Blocked hostname: {hostname}"
            if parsed.hostname:
                for pattern in PRIVATE_IP_PATTERNS:
                    if pattern.match(parsed.hostname):
                        return False, f"Blocked private IP: {parsed.hostname}"
            if '169.254' in url or 'metadata.google' in url:
                return False, "Blocked metadata endpoint"
            return True, ""
        except Exception as e:
            return False, f"Invalid URL: {e}"

    def record_tool_call(self, tool_name: str, tool_input: Any, tool_result: str = ""):
        input_str = json.dumps(tool_input, sort_keys=True) if tool_input else ""
        input_hash = hashlib.sha256(input_str.encode()).hexdigest()[:16]
        record = ToolCallRecord(
            tool_name=tool_name,
            tool_input_hash=input_hash,
            tool_result=tool_result,
            timestamp=time.time(),
        )
        self._tool_history.append(record)
        self._last_tool_results.append(tool_result)
        max_history = self.config.same_tool_failure_halt_after * 3
        if len(self._tool_history) > max_history:
            self._tool_history = self._tool_history[-max_history:]

    def check_loop(self) -> dict:
        if not self._tool_history:
            return {"is_loop": False, "warnings": [], "should_stop": False, "reason": ""}
        
        warnings = []
        should_stop = False
        reason = ""
        
        recent_calls = [r.tool_input_hash for r in self._tool_history[-5:]]
        exact_count = sum(1 for h in recent_calls if h == recent_calls[-1])
        
        if exact_count >= self.config.exact_failure_warn_after:
            warnings.append(f"Exact loop detected: {exact_count} identical calls")
        if exact_count >= self.config.exact_failure_block_after and self.config.hard_stop_enabled:
            should_stop = True
            reason = f"Hard stop: {exact_count} identical calls"
        
        recent_tools = [r.tool_name for r in self._tool_history[-10:]]
        same_tool_count = sum(1 for t in recent_tools if t == recent_tools[-1])
        
        if same_tool_count >= self.config.same_tool_failure_warn_after:
            warnings.append(f"Tool repetition: {same_tool_count} calls to {recent_tools[-1]}")
        if same_tool_count >= self.config.same_tool_failure_halt_after and self.config.hard_stop_enabled:
            should_stop = True
            reason = f"Hard stop: {same_tool_count} calls to {recent_tools[-1]}"
        
        if len(self._last_tool_results) >= 3:
            recent_results = self._last_tool_results[-3:]
            if len(set(recent_results)) == 1 and recent_results[-1]:
                warnings.append("No progress detected: same result repeated")
        
        return {"is_loop": should_stop or len(warnings) > 0, "warnings": warnings, "should_stop": should_stop, "reason": reason}

    def get_tool_category(self, tool_name: str) -> str:
        if tool_name in self.config.idempotent_tools:
            return "idempotent"
        elif tool_name in self.config.mutating_tools:
            return "mutating"
        return "unknown"

    def reset(self):
        self._tool_history = []
        self._last_tool_results = []


_guardrails: Optional[ToolGuardrails] = None
_guardrails_lock = threading.Lock()


def get_tool_guardrails() -> ToolGuardrails:
    global _guardrails
    with _guardrails_lock:
        if _guardrails is None:
            _guardrails = ToolGuardrails()
            logger.info("[guardrails] Initialized global tool guardrails")
        return _guardrails