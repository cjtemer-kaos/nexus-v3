"""
F18: Retry with Exponential Backoff

Per-task retry configuration with configurable backoff.
"""

import asyncio
import logging
import random
from typing import Dict, Optional, Callable, Any
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("nexus-retry")


@dataclass
class RetryConfig:
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_errors: list = None

    def __post_init__(self):
        if self.retryable_errors is None:
            self.retryable_errors = ["timeout", "connection", "rate_limit", "service_unavailable"]


@dataclass
class RetryHistory:
    task_id: str
    attempts: int = 0
    last_error: str = ""
    delays: list = field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    success: bool = False


class RetryManager:
    """Handles retry with exponential backoff for tasks"""

    def __init__(self, default_config: Optional[RetryConfig] = None):
        self.default_config = default_config or RetryConfig()
        self._configs: Dict[str, RetryConfig] = {}
        self._history: Dict[str, RetryHistory] = {}

    def configure(self, task_type: str, **kwargs):
        config = self._configs.get(task_type, RetryConfig())
        for key, value in kwargs.items():
            if hasattr(config, key):
                setattr(config, key, value)
        self._configs[task_type] = config

    async def execute_with_retry(self, task_id: str, func: Callable, *args, config: Optional[RetryConfig] = None, **kwargs) -> Any:
        cfg = config or self._configs.get(task_id, self.default_config)
        history = RetryHistory(task_id=task_id, started_at=datetime.now().isoformat())
        self._history[task_id] = history

        last_error = None
        for attempt in range(cfg.max_retries + 1):
            history.attempts = attempt + 1
            try:
                result = await func(*args, **kwargs)
                history.success = True
                history.completed_at = datetime.now().isoformat()
                return result
            except Exception as e:
                last_error = str(e)
                history.last_error = last_error

                # Check if error is retryable
                if not any(re in last_error.lower() for re in cfg.retryable_errors):
                    raise

                if attempt >= cfg.max_retries:
                    break

                # Calculate delay with exponential backoff
                delay = cfg.base_delay * (cfg.exponential_base ** attempt)
                delay = min(delay, cfg.max_delay)

                # Add jitter
                if cfg.jitter:
                    delay = delay * (0.5 + random.random() * 0.5)

                history.delays.append(delay)
                logger.warning(f"Task {task_id} attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s")
                await asyncio.sleep(delay)

        history.completed_at = datetime.now().isoformat()
        raise Exception(f"Task {task_id} failed after {history.attempts} attempts: {last_error}")

    def get_history(self, task_id: str) -> Optional[RetryHistory]:
        return self._history.get(task_id)

    def get_stats(self) -> Dict:
        total = len(self._history)
        success = sum(1 for h in self._history.values() if h.success)
        failed = total - success
        avg_attempts = sum(h.attempts for h in self._history.values()) / max(total, 1)

        return {
            "total_retries": total,
            "successful": success,
            "failed": failed,
            "success_rate": round((success / max(total, 1)) * 100, 1),
            "avg_attempts": round(avg_attempts, 1),
            "configured_tasks": len(self._configs),
        }
