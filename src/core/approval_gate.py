"""
F7: Approval Gates (HITL)

Human approval between execution rounds with timeout/escalation.
"""

import asyncio
import logging
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from src.core.bounded_history import BoundedHistory

logger = logging.getLogger("nexus-approval")


# Greenlight Gate — canonical gated actions (hermes-workspace parity)
# Interno = free, externo/destructivo = gated. Requiere aprobacion humana explicita.
GATED_ACTIONS: frozenset[str] = frozenset({
    "git push --force",
    "git push -f",
    "pr_merge",
    "pr_close",
    "issue_close",
    "release",
    "npm_publish",
    "publish",
    "public_post",        # X/Discord/blog
    "financial_transaction",
    "destructive_file",   # rm -rf / unlink masivo
    "restart_core_service",
    "force_push",
    "merge",
})

def is_gated_action(action: str) -> bool:
    """True si la accion requiere greenlight humano segun lista canonica."""
    a = (action or "").strip().lower()
    if not a:
        return False
    if a in GATED_ACTIONS:
        return True
    # substring match para comandos compuestos
    for gated in GATED_ACTIONS:
        if gated in a:
            return True
    return False


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    ESCALATED = "escalated"


@dataclass
class ApprovalRequest:
    id: str
    task: str
    description: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = ""
    responded_at: str = ""
    response_by: str = ""
    response_comment: str = ""
    timeout_seconds: int = 300
    escalation_policy: str = "reject"

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def is_expired(self) -> bool:
        created = datetime.fromisoformat(self.created_at)
        elapsed = (datetime.now() - created).total_seconds()
        return elapsed > self.timeout_seconds



class ApprovalGate:
    """Human-in-the-loop approval system"""

    def __init__(self, default_timeout: int = 300):
        self.default_timeout = default_timeout
        self._requests: Dict[str, ApprovalRequest] = {}
        self._callbacks: List[Callable] = []
        self._history = BoundedHistory(maxlen=10000)

    def register_notification_callback(self, callback: Callable):
        """Register callback for new approval requests"""
        self._callbacks.append(callback)

    async def request_approval(self, task: str, description: str, timeout: int = None, escalation: str = "reject") -> ApprovalRequest:
        import uuid
        req = ApprovalRequest(
            id=str(uuid.uuid4())[:8],
            task=task,
            description=description,
            timeout_seconds=timeout or self.default_timeout,
            escalation_policy=escalation,
        )
        self._requests[req.id] = req

        # Notify callbacks
        for cb in self._callbacks:
            try:
                await cb(req)
            except Exception as e:
                logger.error(f"Approval notification callback error: {e}")

        logger.info(f"Approval requested: {req.id} - {task[:60]}")
        return req

    async def respond(self, request_id: str, approved: bool, responder: str = "human", comment: str = "") -> bool:
        req = self._requests.get(request_id)
        if not req:
            return False

        req.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        req.responded_at = datetime.now().isoformat()
        req.response_by = responder
        req.response_comment = comment

        self._history.append({
            "id": req.id,
            "task": req.task,
            "status": req.status.value,
            "responder": responder,
            "response_time": (datetime.fromisoformat(req.responded_at) - datetime.fromisoformat(req.created_at)).total_seconds(),
        })

        logger.info(f"Approval {req.status.value}: {req.id} by {responder}")
        return True

    async def wait_for_approval(self, request_id: str, poll_interval: float = 1.0) -> ApprovalRequest:
        """Wait for approval response with timeout"""
        req = self._requests.get(request_id)
        if not req:
            raise ValueError(f"Unknown request: {request_id}")

        while req.status == ApprovalStatus.PENDING:
            if req.is_expired():
                req.status = ApprovalStatus.TIMED_OUT
                req.responded_at = datetime.now().isoformat()

                # Apply escalation policy
                if req.escalation_policy == "auto_approve":
                    req.status = ApprovalStatus.APPROVED
                    req.response_comment = "Auto-approved (timeout escalation)"
                    logger.info(f"Approval auto-approved (timeout): {req.id}")
                elif req.escalation_policy == "reject":
                    req.response_comment = "Auto-rejected (timeout escalation)"
                    logger.info(f"Approval auto-rejected (timeout): {req.id}")

                break

            await asyncio.sleep(poll_interval)

        return req

    def get_pending_requests(self) -> List[ApprovalRequest]:
        return [r for r in self._requests.values() if r.status == ApprovalStatus.PENDING]

    def get_request(self, request_id: str) -> Optional[ApprovalRequest]:
        return self._requests.get(request_id)

    def get_stats(self) -> Dict:
        total = len(self._requests)
        pending = sum(1 for r in self._requests.values() if r.status == ApprovalStatus.PENDING)
        approved = sum(1 for r in self._requests.values() if r.status == ApprovalStatus.APPROVED)
        rejected = sum(1 for r in self._requests.values() if r.status == ApprovalStatus.REJECTED)
        timed_out = sum(1 for r in self._requests.values() if r.status == ApprovalStatus.TIMED_OUT)

        avg_response = 0
        if self._history:
            avg_response = sum(h["response_time"] for h in self._history) / len(self._history)

        return {
            "total_requests": total,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "timed_out": timed_out,
            "avg_response_time_seconds": round(avg_response, 1),
            "default_timeout": self.default_timeout,
        }
