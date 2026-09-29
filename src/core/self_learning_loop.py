from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from src.core.actor_base import Actor, ActorMessage, ActorResult, ActorState

logger = logging.getLogger(__name__)


@dataclass
class LearningRecord:
    task: str = ""
    model_used: str = ""
    outcome: str = ""
    quality_score: float = 0.0
    pattern: str = ""
    timestamp: float = 0.0
    metadata: dict = field(default_factory=dict)


_self_learning_singleton: SelfLearningLoop | None = None


def get_self_learning_loop() -> SelfLearningLoop:
    """Get or create the singleton SelfLearningLoop instance."""
    global _self_learning_singleton
    if _self_learning_singleton is None:
        _self_learning_singleton = SelfLearningLoop()
    return _self_learning_singleton


class SelfLearningLoop(Actor):
    name = "self_learning_loop"

    def __init__(self, judge_fn: Callable | None = None,
                 memory_store_fn: Callable | None = None,
                 adaptive_router: Any = None,
                 interval_s: float = 120.0,
                 actor_id: str = ""):
        super().__init__(actor_id=actor_id)
        self._judge_fn = judge_fn
        self._memory_store_fn = memory_store_fn
        self._adaptive_router = adaptive_router
        self._interval_s = interval_s
        self._records: list[LearningRecord] = []
        self._last_cycle: float = 0.0
        self._cycles: int = 0
        self._loop_task: asyncio.Task | None = None
        # Persistencia en SQLite
        from pathlib import Path
        self._db_path = Path.home() / ".nexus" / "brain" / "learning_loop.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Crea tabla para records persistentes."""
        import sqlite3
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS learning_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task TEXT, model_used TEXT, outcome TEXT,
            quality_score REAL, pattern TEXT, timestamp REAL,
            processed INTEGER DEFAULT 0
        )""")
        conn.commit()
        conn.close()
    
    def _persist_record(self, record: LearningRecord):
        """Guarda record en SQLite."""
        import sqlite3
        conn = sqlite3.connect(str(self._db_path), timeout=10)
        c = conn.cursor()
        c.execute("INSERT INTO learning_records (task, model_used, outcome, quality_score, pattern, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                  (record.task, record.model_used, record.outcome, record.quality_score, record.pattern, record.timestamp))
        conn.commit()
        conn.close()

    async def on_start(self):
        self._loop_task = asyncio.create_task(self._learning_loop(), name=f"self-learn:{self.actor_id}")

    async def on_stop(self):
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await asyncio.wait_for(self._loop_task, timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    async def _learning_loop(self):
        while self.state != ActorState.STOPPED:
            try:
                await asyncio.sleep(self._interval_s)
                await self._cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("SelfLearning cycle error: %s", e)

    async def _cycle(self):
        self._cycles += 1
        self._last_cycle = time.time()

        if not self._records:
            logger.debug("SelfLearning: no records to process")
            return

        recent = self._records[:50]
        self._records = self._records[50:]

        for record in recent:
            if self._judge_fn:
                try:
                    judge_result = self._judge_fn(record.task, record.outcome)
                    if asyncio.iscoroutine(judge_result):
                        judge_result = await judge_result
                    if hasattr(judge_result, 'action') and hasattr(judge_result, 'confidence'):
                        multiplier = 1.0 if judge_result.action == "ACCEPT" else 0.3
                        record.quality_score = judge_result.confidence * multiplier
                    elif hasattr(judge_result, 'confidence'):
                        record.quality_score = judge_result.confidence
                except Exception as e:
                    logger.debug("Judge failed: %s", e)

            if self._adaptive_router and record.model_used:
                self._adaptive_router.record_result(
                    model=record.model_used,
                    success=record.quality_score > 0.6,
                    quality_score=record.quality_score,
                )

            record.pattern = self._extract_pattern(record)
            self._persist_record(record)  # Persistir en SQLite

            if self._memory_store_fn and record.pattern:
                try:
                    self._memory_store_fn(
                        f"self_learning_{int(record.timestamp)}",
                        record.pattern,
                    )
                except Exception as e:
                    logger.debug("Memory store failed: %s", e)

        logger.info("SelfLearning cycle %d: processed %d records", self._cycles, len(recent))

    def _extract_pattern(self, record: LearningRecord) -> str:
        parts = []
        if record.quality_score > 0.8:
            parts.append("high_quality")
        elif record.quality_score < 0.3:
            parts.append("low_quality")

        lower_task = record.task.lower()
        if any(kw in lower_task for kw in ["código", "programar", "python"]):
            parts.append("coding")
        if any(kw in lower_task for kw in ["investigar", "research"]):
            parts.append("research")
        if any(kw in lower_task for kw in ["docker", "despliegue"]):
            parts.append("devops")

        if parts:
            return f"[{'|'.join(parts)}] model={record.model_used} score={record.quality_score:.2f}"
        return ""

    async def handle_message(self, msg: ActorMessage) -> ActorResult:
        if msg.msg_type == "learn":
            try:
                data = json.loads(msg.content) if isinstance(msg.content, str) else msg.content
                record = LearningRecord(
                    task=data.get("task", ""),
                    model_used=data.get("model", ""),
                    outcome=data.get("outcome", ""),
                    quality_score=data.get("quality", 0.5),
                    timestamp=time.time(),
                    metadata=data.get("metadata", {}),
                )
                self._records.append(record)
                return ActorResult(success=True, content=f"Recorded learning: {record.task[:40]}")
            except Exception as e:
                return ActorResult(success=False, content="", error=str(e))

        if msg.msg_type == "cycle":
            await self._cycle()
            return ActorResult(success=True, content=f"Cycle {self._cycles} done, {len(self._records)} pending")

        if msg.msg_type == "status":
            return ActorResult(success=True, content=json.dumps({
                "cycles": self._cycles,
                "pending_records": len(self._records),
                "last_cycle": self._last_cycle,
            }))

        return ActorResult(success=False, content="", error=f"Unknown msg_type: {msg.msg_type}")

    def get_stats(self) -> dict:
        """Return current learning loop statistics."""
        return {
            "status": "running" if self._loop_task and not self._loop_task.done() else "stopped",
            "cycles": self._cycles,
            "pending_records": len(self._records),
            "last_cycle": self._last_cycle,
            "has_judge": self._judge_fn is not None,
            "has_memory": self._memory_store_fn is not None,
            "has_router": self._adaptive_router is not None,
        }
