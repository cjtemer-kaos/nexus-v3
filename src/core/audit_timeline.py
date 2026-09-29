"""
Audit Timeline — Structured event sourcing with severity levels.

Pattern extracted from openclaw audit-timeline.ts.
Provides event recording, filtering, deduplication, and severity aggregation.
Sources: snapshot, monitor, approval, operation, handoff, system.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("nexus-audit")

AUDIT_SEVERITIES = ["info", "warn", "action-required", "error"]
AUDIT_SOURCES = ["snapshot", "monitor", "approval", "operation", "handoff", "system"]


class AuditSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"
    ACTION_REQUIRED = "action-required"
    ERROR = "error"


@dataclass
class AuditEvent:
    timestamp: str = ""
    severity: AuditSeverity = AuditSeverity.INFO
    source: str = "system"
    actor: str = ""
    message: str = ""
    details: Dict = field(default_factory=dict)
    event_id: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()


@dataclass
class AuditTimelineSnapshot:
    generated_at: str = ""
    events: List[AuditEvent] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now().isoformat()


class AuditTimeline:
    def __init__(self, max_events: int = 1000, persist_path: str = ""):
        self._events: List[AuditEvent] = []
        self._max_events = max_events
        self._persist_path = persist_path
        if persist_path:
            Path(persist_path).parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self):
        try:
            if os.path.exists(self._persist_path):
                with open(self._persist_path, "r") as f:
                    data = json.load(f)
                for e in data.get("events", []):
                    self._events.append(AuditEvent(**e))
                logger.info(f"Loaded {len(self._events)} audit events from {self._persist_path}")
        except Exception as e:
            logger.warning(f"Could not load audit events: {e}")

    def _save(self):
        if not self._persist_path:
            return
        try:
            data = {
                "generated_at": datetime.now().isoformat(),
                "events": [
                    {
                        "timestamp": e.timestamp,
                        "severity": e.severity.value if isinstance(e.severity, AuditSeverity) else e.severity,
                        "source": e.source,
                        "actor": e.actor,
                        "message": e.message,
                        "details": e.details,
                        "event_id": e.event_id,
                    }
                    for e in self._events[-self._max_events:]
                ],
            }
            atomic_write_json(self._persist_path, data)
        except Exception as e:
            logger.warning(f"Could not persist audit events: {e}")

    def record(
        self,
        severity: AuditSeverity,
        source: str,
        message: str,
        actor: str = "",
        details: Optional[Dict] = None,
    ) -> AuditEvent:
        if source not in AUDIT_SOURCES:
            source = "system"
        import uuid
        event = AuditEvent(
            severity=severity,
            source=source,
            actor=actor,
            message=message,
            details=details or {},
            event_id=str(uuid.uuid4())[:8],
        )
        self._events.append(event)
        if len(self._events) > self._max_events * 2:
            self._events = self._events[-self._max_events:]
        log_fn = logger.warning if severity in (AuditSeverity.WARN, AuditSeverity.ACTION_REQUIRED) else (logger.error if severity == AuditSeverity.ERROR else logger.info)
        log_fn(f"AUDIT [{severity.value}] [{source}] {message}")
        self._save()
        return event

    def info(self, source: str, message: str, actor: str = "", details: Optional[Dict] = None) -> AuditEvent:
        return self.record(AuditSeverity.INFO, source, message, actor, details)

    def warn(self, source: str, message: str, actor: str = "", details: Optional[Dict] = None) -> AuditEvent:
        return self.record(AuditSeverity.WARN, source, message, actor, details)

    def action_required(self, source: str, message: str, actor: str = "", details: Optional[Dict] = None) -> AuditEvent:
        return self.record(AuditSeverity.ACTION_REQUIRED, source, message, actor, details)

    def error(self, source: str, message: str, actor: str = "", details: Optional[Dict] = None) -> AuditEvent:
        return self.record(AuditSeverity.ERROR, source, message, actor, details)

    def snapshot(self) -> AuditTimelineSnapshot:
        counts = {}
        for sev in AUDIT_SEVERITIES:
            counts[sev] = sum(1 for e in self._events if e.severity.value == sev)
        return AuditTimelineSnapshot(
            events=self._events[-100:].copy(),
            counts=counts,
        )

    def filter(
        self,
        severity: Optional[AuditSeverity] = None,
        source: Optional[str] = None,
        actor: Optional[str] = None,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[AuditEvent]:
        result = self._events
        if severity:
            result = [e for e in result if e.severity == severity or e.severity.value == severity]
        if source:
            result = [e for e in result if e.source == source]
        if actor:
            result = [e for e in result if e.actor == actor]
        if since:
            result = [e for e in result if e.timestamp >= since]
        return result[-limit:]

    def get_event_count(self) -> int:
        return len(self._events)

    def get_counts(self) -> Dict[str, int]:
        counts = {}
        for sev in AUDIT_SEVERITIES:
            counts[sev] = sum(1 for e in self._events if e.severity.value == sev)
        return counts

    def clear(self):
        self._events = []
        self._save()
