"""Salience tracker — emotional salience for recall bias.

Port verbatim de lethe/src/lethe/memory/salience.py adaptado a Nexus.
Sin lethe.paths/prompts — usa workspace local + classifier opcional (LLM).
Si no hay classifier, es no-op seguro.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

def _workspace_dir() -> Path:
    root = os.environ.get("NEXUS_ROOT", "") or str(Path(__file__).parents[2])
    ws = Path(root) / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    return ws

SALIENCE_TAGS_FILE = str(_workspace_dir() / "emotional_tags.md")
HIGH_AROUSAL_THRESHOLD = 0.75
TAG_LOG_MAX_LINES = 300
TAG_LOG_KEEP_LINES = 140
FLASHBACK_LOOKBACK = 12

SALIENCE_USER_PROMPT_FALLBACK = (
    "Classify the most recent user signals.\n"
    "- valence in [-1,1], arousal/confidence in [0,1]\n"
    "- capture sarcasm and mixed affect\n"
    "- output max 8 items as JSON array only\n\n"
    "Previous state summary:\n{previous_state}\n\n"
    "Recent signals:\n{recent_signals}\n"
)


class SalienceTracker:
    """Tracks emotional salience independently from associative recall."""

    def __init__(
        self,
        classifier: Optional[Callable[[str], Awaitable[str]]] = None,
        tags_file: str = SALIENCE_TAGS_FILE,
    ):
        self.classifier = classifier
        self.tags_file = tags_file
        self._stats = {
            "tags_total": 0,
            "tags_high_arousal": 0,
            "last_tagged_at": "",
            "last_error": "",
            "tags_pruned_total": 0,
        }
        self._active_patterns: deque[str] = deque(maxlen=FLASHBACK_LOOKBACK)

    @property
    def enabled(self) -> bool:
        return self.classifier is not None

    @property
    def active_patterns(self) -> list[str]:
        return list(self._active_patterns)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    async def tag(self, message) -> None:
        if not self.classifier:
            return
        try:
            text = self._extract_text(message)
            if not text or len(text) < 5:
                return
            prev = self.read_tags_tail(max_lines=30) or "(none)"
            prompt = SALIENCE_USER_PROMPT_FALLBACK.format(
                previous_state=prev,
                recent_signals=text[:2000],
            )
            raw = await self.classifier(prompt)
            if not raw:
                return
            tags = self.parse_tags(raw)
            if not tags:
                return
            self.append_tags(tags)
            self.update_active_patterns(tags)
            self._stats["tags_total"] += len(tags)
            self._stats["tags_high_arousal"] += sum(1 for t in tags if t.get("high_arousal"))
            self._stats["last_tagged_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
            logger.warning("Salience tagging failed: %s", e)
            self._stats["last_error"] = str(e)

    @staticmethod
    def _extract_text(message) -> str:
        if isinstance(message, list):
            parts = []
            for m in message:
                if isinstance(m, dict):
                    parts.append(m.get("content") or m.get("text") or "")
                else:
                    parts.append(str(m))
            return "\n".join(parts)
        if isinstance(message, dict):
            return message.get("content") or message.get("text") or str(message)
        return str(message) if message else ""

    @staticmethod
    def parse_tags(raw: str) -> list[dict]:
        try:
            # Extraer JSON array del texto
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return []
            arr = json.loads(m.group(0))
            if not isinstance(arr, list):
                return []
            out = []
            for item in arr[:8]:
                if not isinstance(item, dict):
                    continue
                # Normalizar campos
                valence = float(item.get("valence", 0))
                arousal = float(item.get("arousal", item.get("confidence", 0)))
                out.append({
                    "label": str(item.get("label") or item.get("emotion") or "neutral")[:40],
                    "valence": max(-1, min(1, valence)),
                    "arousal": max(0, min(1, arousal)),
                    "high_arousal": arousal >= HIGH_AROUSAL_THRESHOLD,
                    "confidence": max(0, min(1, float(item.get("confidence", arousal)))),
                })
            return out
        except Exception:
            return []

    def append_tags(self, tags: list[dict]) -> None:
        try:
            Path(self.tags_file).parent.mkdir(parents=True, exist_ok=True)
            lines = []
            ts = datetime.now(timezone.utc).isoformat()
            for t in tags:
                lines.append(f"- [{ts}] {t['label']} valence={t['valence']:.2f} arousal={t['arousal']:.2f} high={t['high_arousal']}")
            with open(self.tags_file, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
            self._prune_if_needed()
            # Emit event
            try:
                from src.observability.event_stream import emit, EventType
                emit(EventType.SALIENCE_TAGGED, data={"tags": tags}, source="salience")
            except Exception:
                pass
        except Exception as e:
            logger.warning("append_tags failed: %s", e)

    def _prune_if_needed(self) -> None:
        try:
            if not os.path.exists(self.tags_file):
                return
            lines = Path(self.tags_file).read_text(encoding="utf-8").splitlines()
            if len(lines) > TAG_LOG_MAX_LINES:
                keep = lines[-TAG_LOG_KEEP_LINES:]
                Path(self.tags_file).write_text("\n".join(keep) + "\n", encoding="utf-8")
                self._stats["tags_pruned_total"] += len(lines) - len(keep)
        except Exception:
            pass

    def update_active_patterns(self, tags: list[dict]) -> None:
        for t in tags:
            if t.get("high_arousal"):
                self._active_patterns.append(t["label"])

    def read_tags_tail(self, max_lines: int = 30) -> str:
        try:
            if not os.path.exists(self.tags_file):
                return ""
            lines = Path(self.tags_file).read_text(encoding="utf-8").splitlines()
            return "\n".join(lines[-max_lines:])
        except Exception:
            return ""

    def recall_bias(self) -> dict:
        """Bias para retrieval: patrones activos + stats para ponderar recall."""
        return {
            "active_patterns": list(self._active_patterns),
            "high_arousal_ratio": (
                self._stats["tags_high_arousal"] / max(1, self._stats["tags_total"])
            ),
            "enabled": self.enabled,
        }
