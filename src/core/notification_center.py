"""
Notification Center — Ack-based notification system with TTL, snooze, and stale pruning.

Pattern extracted from openclaw notification-center.ts.
Agents can send notifications, acknowledge them, snooze, and auto-prune stale entries.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("nexus-notify")


class NotificationValidationError(Exception):
    def __init__(self, message: str, issues: List[str] = None, status_code: int = 400):
        self.issues = issues or []
        self.status_code = status_code
        super().__init__(f"{message} | issues: {self.issues}")


@dataclass
class Notification:
    item_id: str = ""
    source: str = ""
    target: str = ""
    title: str = ""
    body: str = ""
    severity: str = "info"
    created_at: str = ""
    ttl_minutes: float = 0
    acknowledged: bool = False
    acknowledged_at: str = ""
    acknowledged_by: str = ""
    snoozed_until: str = ""

    def __post_init__(self):
        import uuid
        if not self.item_id:
            self.item_id = str(uuid.uuid4())[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    @property
    def is_expired(self) -> bool:
        if self.ttl_minutes <= 0:
            return False
        try:
            created = datetime.fromisoformat(self.created_at)
            elapsed = (datetime.now() - created).total_seconds() / 60
            return elapsed > self.ttl_minutes
        except Exception:
            return False

    @property
    def is_snoozed(self) -> bool:
        if not self.snoozed_until:
            return False
        try:
            return datetime.now() < datetime.fromisoformat(self.snoozed_until)
        except Exception:
            return False


@dataclass
class AcknowledgeInput:
    item_id: str
    note: str = ""
    ttl_minutes: float = 0
    snooze_until: str = ""


class NotificationCenter:
    def __init__(self, persist_path: str = ""):
        self._notifications: Dict[str, Notification] = {}
        self._persist_path = persist_path
        if persist_path:
            Path(persist_path).parent.mkdir(parents=True, exist_ok=True)
            self._load()

    def _load(self):
        try:
            if os.path.exists(self._persist_path):
                with open(self._persist_path, "r") as f:
                    data = json.load(f)
                for n in data.get("notifications", []):
                    notif = Notification(**n)
                    self._notifications[notif.item_id] = notif
                logger.info(f"Loaded {len(self._notifications)} notifications")
        except Exception as e:
            logger.warning(f"Could not load notifications: {e}")

    def _save(self):
        if not self._persist_path:
            return
        try:
            data = {
                "updated_at": datetime.now().isoformat(),
                "notifications": [
                    {
                        "item_id": n.item_id,
                        "source": n.source,
                        "target": n.target,
                        "title": n.title,
                        "body": n.body,
                        "severity": n.severity,
                        "created_at": n.created_at,
                        "ttl_minutes": n.ttl_minutes,
                        "acknowledged": n.acknowledged,
                        "acknowledged_at": n.acknowledged_at,
                        "acknowledged_by": n.acknowledged_by,
                        "snoozed_until": n.snoozed_until,
                    }
                    for n in self._notifications.values()
                ],
            }
            atomic_write_json(self._persist_path, data)
        except Exception as e:
            logger.warning(f"Could not persist notifications: {e}")

    def send(
        self,
        source: str,
        target: str,
        title: str,
        body: str = "",
        severity: str = "info",
        ttl_minutes: float = 0,
    ) -> Notification:
        issues = []
        if not source:
            issues.append("source is required")
        if not target:
            issues.append("target is required")
        if not title:
            issues.append("title is required")
        if issues:
            raise NotificationValidationError("Invalid notification", issues)
        notif = Notification(
            source=source,
            target=target,
            title=title,
            body=body,
            severity=severity if severity in ("info", "warn", "error", "critical") else "info",
            ttl_minutes=max(0, ttl_minutes),
        )
        self._notifications[notif.item_id] = notif
        logger.info(f"Notification [{severity}] {source} -> {target}: {title}")
        self._save()
        return notif

    def acknowledge(self, input_data: AcknowledgeInput, by: str = "") -> Notification:
        notif = self._notifications.get(input_data.item_id)
        if not notif:
            raise NotificationValidationError("Notification not found", [f"No notification with id {input_data.item_id}"], 404)
        notif.acknowledged = True
        notif.acknowledged_at = datetime.now().isoformat()
        notif.acknowledged_by = by
        if input_data.note:
            notif.body = notif.body + f" | ack note: {input_data.note}"
        if input_data.ttl_minutes > 0:
            notif.ttl_minutes = input_data.ttl_minutes
        if input_data.snooze_until:
            notif.snoozed_until = input_data.snooze_until
        logger.info(f"Notification acknowledged: {input_data.item_id} by {by}")
        self._save()
        return notif

    def snooze(self, item_id: str, until: str) -> Notification:
        notif = self._notifications.get(item_id)
        if not notif:
            raise NotificationValidationError("Notification not found", status_code=404)
        notif.snoozed_until = until
        self._save()
        return notif

    def get_pending(self, target: str = "", severity: str = "") -> List[Notification]:
        result = []
        for n in self._notifications.values():
            if n.acknowledged:
                continue
            if n.is_expired:
                continue
            if n.is_snoozed:
                continue
            if target and n.target != target and n.target != "*":
                continue
            if severity and n.severity != severity:
                continue
            result.append(n)
        result.sort(key=lambda x: x.created_at, reverse=True)
        return result

    def get_by_target(self, target: str) -> List[Notification]:
        return [n for n in self._notifications.values() if n.target == target or n.target == "*"]

    def prune_stale(self, dry_run: bool = False) -> Dict:
        before = len(self._notifications)
        stale_ids = []
        for item_id, n in list(self._notifications.items()):
            if n.is_expired:
                stale_ids.append(item_id)
                if not dry_run:
                    del self._notifications[item_id]
        after = len(self._notifications) if not dry_run else before
        result = {
            "dry_run": dry_run,
            "before": before,
            "removed": len(stale_ids),
            "after": after,
            "removed_ids": stale_ids,
        }
        if not dry_run:
            self._save()
        return result

    def get_stats(self) -> Dict:
        total = len(self._notifications)
        pending = len(self.get_pending())
        acknowledged = sum(1 for n in self._notifications.values() if n.acknowledged)
        expired = sum(1 for n in self._notifications.values() if n.is_expired)
        return {
            "total": total,
            "pending": pending,
            "acknowledged": acknowledged,
            "expired": expired,
            "by_severity": {
                sev: sum(1 for n in self._notifications.values() if n.severity == sev)
                for sev in ("info", "warn", "error", "critical")
            },
        }
