"""
Retry utilities — jittered backoff for decorrelated retries.

Adaptado para SuperNEXUS v2.0
"""

import random
import threading
import time


_jitter_counter = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay."""
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


def get_retry_delay(attempt: int, error_type: str = "default") -> float:
    """Get appropriate delay based on error type."""
    delays = {
        "default": (5.0, 120.0),
        "auth": (2.0, 30.0),
        "rate_limit": (30.0, 300.0),
        "server_error": (10.0, 60.0),
        "timeout": (5.0, 45.0),
    }
    base, max_d = delays.get(error_type, delays["default"])
    return jittered_backoff(attempt, base_delay=base, max_delay=max_d)