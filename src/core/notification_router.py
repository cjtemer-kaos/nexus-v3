from __future__ import annotations

from datetime import datetime, timezone
from src.core.notification_signals import (
    NotificationSource,
    UserNotificationSignal,
    notification_category_from_metadata,
    notification_origin_from_metadata,
    notification_source_from_name,
    notification_urgency_from_metadata,
)


class NotificationRouter:
    """Turns background events into typed notification signals (lethe parity, sin Actor)."""

    BACKGROUND_SOURCES = {
        NotificationSource.BRAINSTEM,
        NotificationSource.DMN,
    }

    def __init__(self, principal_id: str = "user"):
        self.principal_id = principal_id

    def from_event(self, event: dict) -> UserNotificationSignal | None:
        """Accept dict {id, source_name, payload:{recipient,message,metadata,kind}, created_at?}"""
        payload = event.get("payload") or {}
        if payload.get("recipient") not in (None, self.principal_id, "user"):
            # if recipient present and mismatched → drop
            if payload.get("recipient") is not None:
                return None
        content = str(payload.get("message", event.get("message", ""))).strip()
        if not content:
            return None
        metadata = dict(payload.get("metadata") or event.get("metadata") or {})
        kind = str(metadata.get("kind", payload.get("kind", event.get("kind", "")))).strip()
        source_name = str(event.get("source_name", event.get("actor_name", ""))).strip()
        source = notification_source_from_name(source_name or "dmn")
        if source not in self.BACKGROUND_SOURCES and source_name:
            # allow dmn/brainstem only; unknown source → treat as dmn if no name
            pass
        category = notification_category_from_metadata(metadata, kind)
        urgency = notification_urgency_from_metadata(metadata, category, kind)
        origin = notification_origin_from_metadata(metadata, source, kind)
        created_at = event.get("created_at") or datetime.now(timezone.utc)
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at)
            except Exception:
                created_at = datetime.now(timezone.utc)
        return UserNotificationSignal(
            event_id=str(event.get("id", event.get("event_id", "evt"))),
            source=source,
            source_name=source_name or source.value,
            source_actor_id=str(event.get("actor_id", event.get("source_actor_id", ""))),
            origin=origin,
            category=category,
            urgency=urgency,
            content=content,
            kind=kind,
            metadata=metadata,
            observed_at=created_at,
        )

    # compat alias lethe
    def from_actor_event(self, event):  # type: ignore[no-untyped-def]
        if hasattr(event, "payload"):
            return self.from_event({"id": getattr(event, "id", ""), "payload": getattr(event, "payload", {}), "source_name": getattr(event, "actor_id", ""), "created_at": getattr(event, "created_at", None)})
        return self.from_event(dict(event))
