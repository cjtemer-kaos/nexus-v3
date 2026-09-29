"""
Agent Handoff Protocol — Structured handoff packets between gemas.

Pattern extracted from openclaw hall-handoff.ts.
Each handoff carries goal, current result, done condition, and ownership chain.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime

logger = logging.getLogger("nexus-handoff")


class HandoffValidationError(Exception):
    def __init__(self, message: str, issues: List[str] = None, status_code: int = 400):
        self.issues = issues or []
        self.status_code = status_code
        super().__init__(f"{message} | issues: {self.issues}")


@dataclass
class ArtifactRef:
    path: str
    description: str = ""
    artifact_type: str = "file"


@dataclass
class HandoffPacket:
    goal: str
    current_result: str
    done_when: str
    next_owner: str
    blockers: List[str] = field(default_factory=list)
    requires_input_from: List[str] = field(default_factory=list)
    artifact_refs: List[ArtifactRef] = field(default_factory=list)
    context_summary: str = ""
    confidence: float = 1.0
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


def _normalize_string_list(values: Optional[List[str]], label: str, max_chars: int) -> List[str]:
    if not values:
        return []
    result = []
    for v in values:
        if not v or not v.strip():
            continue
        v = v.strip()[:max_chars]
        result.append(v)
    return result


def build_handoff_packet(
    goal: str,
    current_result: str,
    done_when: str,
    next_owner: str,
    blockers: Optional[List[str]] = None,
    requires_input_from: Optional[List[str]] = None,
    artifact_refs: Optional[List[ArtifactRef]] = None,
    context_summary: str = "",
    confidence: float = 1.0,
) -> HandoffPacket:
    issues = []

    if not goal or not goal.strip():
        issues.append("goal is required (max 240 chars)")
    if not current_result or not current_result.strip():
        issues.append("current_result is required (max 500 chars)")
    if not done_when or not done_when.strip():
        issues.append("done_when is required (max 240 chars)")
    if not next_owner or not next_owner.strip():
        issues.append("next_owner is required (max 120 chars)")

    if issues:
        raise HandoffValidationError("Invalid handoff payload", issues)

    goal = goal.strip()[:240]
    current_result = current_result.strip()[:500]
    done_when = done_when.strip()[:240]
    next_owner = next_owner.strip()[:120]

    return HandoffPacket(
        goal=goal,
        current_result=current_result,
        done_when=done_when,
        next_owner=next_owner,
        blockers=_normalize_string_list(blockers, "blockers", 240),
        requires_input_from=_normalize_string_list(requires_input_from, "requires_input_from", 120),
        artifact_refs=artifact_refs or [],
        context_summary=context_summary.strip()[:500] if context_summary else "",
        confidence=min(max(confidence, 0.0), 1.0),
    )


def summarize_handoff(packet: HandoffPacket, max_length: int = 300) -> str:
    parts = [
        f"Goal: {packet.goal}",
        f"Next: {packet.next_owner}",
        f"Done when: {packet.done_when}",
    ]
    if packet.blockers:
        parts.append(f"Blockers: {', '.join(packet.blockers)}")
    if packet.requires_input_from:
        parts.append(f"Needs input from: {', '.join(packet.requires_input_from)}")
    summary = " | ".join(parts)
    if len(summary) > max_length:
        summary = summary[:max_length - 3] + "..."
    return summary


def handoff_packet_to_dict(packet: HandoffPacket) -> Dict:
    return {
        "goal": packet.goal,
        "current_result": packet.current_result,
        "done_when": packet.done_when,
        "next_owner": packet.next_owner,
        "blockers": packet.blockers,
        "requires_input_from": packet.requires_input_from,
        "artifact_refs": [{"path": a.path, "description": a.description, "type": a.artifact_type} for a in packet.artifact_refs],
        "context_summary": packet.context_summary,
        "confidence": packet.confidence,
        "created_at": packet.created_at,
    }
