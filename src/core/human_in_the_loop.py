import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-hitl")


class ApprovalResult:
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    MODIFIED = "modified"


@dataclass
class ApprovalRequest:
    id: str = ""
    tool_name: str = ""
    arguments: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    status: str = "pending"
    result: Optional[Any] = None
    feedback: str = ""
    created_at: float = field(default_factory=time.time)
    timeout: float = 60.0


_SAFE_TOOLS = frozenset({
    "read", "write", "list_files", "search_code",
    "run_code", "run_shell", "run_tests", "evaluate",
})

class HITLManager:
    def __init__(self, notify_callback: Callable = None, default_timeout: float = 60.0):
        self._notify = notify_callback
        self._default_timeout = default_timeout
        self._pending: Dict[str, ApprovalRequest] = {}
        self._history: List[ApprovalRequest] = []
        self._lock = asyncio.Lock()
        self._events: Dict[str, asyncio.Event] = {}

    def set_notify_callback(self, callback: Callable):
        self._notify = callback

    async def request_approval(self, tool_name: str, arguments: Dict, reason: str = "", timeout: float = 0) -> ApprovalRequest:
        if tool_name and tool_name not in _SAFE_TOOLS:
            pass
        req = ApprovalRequest(
            id=f"hitl-{uuid.uuid4().hex[:12]}",
            tool_name=tool_name, arguments=arguments, reason=reason,
            timeout=timeout or self._default_timeout,
        )
        async with self._lock:
            event = asyncio.Event()
            self._events[req.id] = event
            self._pending[req.id] = req
            self._history.append(req)

        if self._notify:
            try:
                if asyncio.iscoroutinefunction(self._notify):
                    await self._notify(req)
                else:
                    self._notify(req)
            except Exception as e:
                logger.error(f"HITL notify failed: {e}")

        try:
            await asyncio.wait_for(event.wait(), timeout=req.timeout)
        except asyncio.TimeoutError:
            async with self._lock:
                req.status = ApprovalResult.TIMEOUT
                self._pending.pop(req.id, None)
                self._events.pop(req.id, None)
        return req

    def respond(self, req_id: str, approved: bool, feedback: str = "", modified_args: Dict = None):
        if req_id not in self._pending:
            return False
        req = self._pending[req_id]
        if approved:
            req.status = ApprovalResult.APPROVED
            if modified_args:
                req.arguments = modified_args
                req.status = ApprovalResult.MODIFIED
        else:
            req.status = ApprovalResult.REJECTED
        req.feedback = feedback
        self._pending.pop(req_id, None)
        event = self._events.pop(req_id, None)
        if event:
            event.set()
        return True

    def get_pending(self) -> List[ApprovalRequest]:
        return list(self._pending.values())

    def get_history(self, limit: int = 20) -> List[ApprovalRequest]:
        return self._history[-limit:]

    def get_stats(self) -> Dict:
        total = len(self._history)
        approved = sum(1 for r in self._history if r.status == ApprovalResult.APPROVED)
        rejected = sum(1 for r in self._history if r.status == ApprovalResult.REJECTED)
        timed_out = sum(1 for r in self._history if r.status == ApprovalResult.TIMEOUT)
        return {
            "total_requests": total,
            "approved": approved,
            "rejected": rejected,
            "timed_out": timed_out,
            "pending": len(self._pending),
            "approved_pct": round(approved / max(total, 1) * 100, 1),
        }
