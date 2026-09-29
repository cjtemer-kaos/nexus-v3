"""Checkpoint Contract — structured output enforcement for LLM gemas.

State enum, validation, and multi-format response parsing.
Used by LLMRoleGema when manifest sets use_checkpoint_contract=true.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class State(str, Enum):
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    NEEDS_INPUT = "needs_input"


# Regex evidence indicators — patterns that suggest concrete output
EVIDENCE_RE = re.compile(
    r"(\d+|#\d+|`[^`]+`|https?://\S+|"
    r"[\w./\\]+\.(?:py|js|ts|rs|go|java|rb|sh|json|yaml|toml|cfg|ini|md)|"
    r"✓|✅|❌|✗|fixed|completed|implemented|created|deleted|tested|ran|built|"
    r"executed|installed|configured|deployed)",
    re.IGNORECASE,
)

# Vague phrases that indicate an insufficient report
VAGUE_PHRASES = [
    "everything works",
    "all good",
    "all done",
    "nothing to report",
    "no issues",
    "looks fine",
    "seems ok",
    "should be fine",
    "probably fine",
    "i think it worked",
    "task completed successfully",
    "done.",
]


@dataclass
class CheckpointReport:
    state: State
    summary: str = ""
    details: str = ""
    blocker: str = ""
    next_steps: str = ""
    evidence: List[str] = field(default_factory=list)
    raw_output: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


def validate_report(report: CheckpointReport) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    # State must be one of the valid states (guaranteed by enum)
    if not isinstance(report.state, State):
        errors.append("state must be a State enum member")

    # Summary required, at least 10 chars
    if len(report.summary.strip()) < 10:
        errors.append("summary too short (min 10 chars)")

    # Reject vague summaries
    summary_lower = report.summary.lower().strip().rstrip(".")
    for vague in VAGUE_PHRASES:
        if summary_lower == vague or summary_lower.endswith(vague):
            errors.append(f"vague summary: '{report.summary}'")
            break

    if not errors and summary_lower in ("done", "completed", "finished", "ok"):
        errors.append(f"vague summary: '{report.summary}'")

    # Blocked must have a blocker
    if report.state == State.BLOCKED and len(report.blocker.strip()) < 5:
        errors.append("BLOCKED state but blocker description too short")

    # DONE must have evidence
    if report.state == State.DONE and not report.evidence:
        errors.append("DONE state but no evidence provided")

    # Check evidence for concrete indicators
    if report.evidence:
        concrete = sum(1 for e in report.evidence if EVIDENCE_RE.search(e))
        if concrete == 0 and len(report.evidence) <= 1:
            errors.append("evidence lacks concrete indicators (paths, numbers, checks)")

    return len(errors) == 0, errors


def parse_llm_response(text: str) -> CheckpointReport:
    parsed = _try_json_fence(text)
    if parsed:
        return _dict_to_report(parsed)

    parsed = _try_inline_json(text)
    if parsed:
        return _dict_to_report(parsed)

    parsed = _try_yaml_ish(text)
    if parsed:
        return _dict_to_report(parsed)

    return _fallback_extraction(text)


def _try_json_fence(text: str) -> Optional[Dict]:
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    return None


def _try_inline_json(text: str) -> Optional[Dict]:
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = text[start:i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
    return None


def _try_yaml_ish(text: str) -> Optional[Dict]:
    keys = {"state", "summary"}
    found: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        for key in keys:
            m = re.match(rf"{key}\s*:\s*(.*)", line, re.IGNORECASE)
            if m:
                val = m.group(1).strip().strip("'\"")
                found[key] = val
    if "state" in found and "summary" in found:
        result: Dict[str, Any] = {"state": found["state"], "summary": found["summary"]}
        for extra_key in ("details", "blocker", "next_steps"):
            for line in text.splitlines():
                m = re.match(rf"{extra_key}\s*:\s*(.*)", line.strip(), re.IGNORECASE)
                if m:
                    result[extra_key] = m.group(1).strip().strip("'\"")
                    break
        return result
    return None


def _dict_to_report(d: Dict) -> CheckpointReport:
    state_str = str(d.get("state", "")).lower().strip()
    try:
        state = State(state_str)
    except ValueError:
        state = State.IN_PROGRESS
        logger.warning(f"checkpoint_contract: unknown state '{state_str}', defaulting to in_progress")

    evidence = d.get("evidence", [])
    if isinstance(evidence, str):
        evidence = [evidence]

    return CheckpointReport(
        state=state,
        summary=str(d.get("summary", "")),
        details=str(d.get("details", "")),
        blocker=str(d.get("blocker", "")),
        next_steps=str(d.get("next_steps", "")),
        evidence=[str(e) for e in evidence] if isinstance(evidence, list) else [],
    )


def _fallback_extraction(text: str) -> CheckpointReport:
    clean = text.strip()
    state = State.DONE
    # Simple heuristics
    if re.search(r"\b(?:blocked|stuck|can'?t|cannot|unable|failed|error)\b", clean, re.IGNORECASE):
        if len(clean) > 50:
            state = State.BLOCKED
    elif re.search(r"\b(?:need|require|waiting|pending|depends)\b", clean, re.IGNORECASE):
        state = State.NEEDS_INPUT

    evidence = []
    for m in EVIDENCE_RE.finditer(clean):
        evidence.append(m.group(0))
    evidence = list(dict.fromkeys(evidence))[:5]

    return CheckpointReport(
        state=state,
        summary=clean[:200],
        details=clean[:500] if len(clean) > 200 else "",
        blocker="extracted from raw output" if state == State.BLOCKED else "",
        evidence=evidence,
    )
