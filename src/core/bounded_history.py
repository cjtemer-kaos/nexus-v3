"""
Bounded History Utility

Shared utility for all modules that need bounded collections with timestamp expiration.
Replaces unbounded lists/dicts to prevent memory leaks.
"""

import time
import threading
from collections import deque
from typing import Any


class BoundedHistory:
    """Thread-safe bounded history with automatic expiration"""

    def __init__(self, maxlen: int = 1000, ttl_seconds: float = 0):
        self._data = deque(maxlen=maxlen)
        self._ttl = ttl_seconds
        self._maxlen = maxlen
        self._lock = threading.RLock()

    def append(self, item: Any):
        with self._lock:
            if self._ttl > 0:
                self._data.append((time.time(), item))
            else:
                self._data.append(item)

    def extend(self, items):
        with self._lock:
            for item in items:
                if self._ttl > 0:
                    self._data.append((time.time(), item))
                else:
                    self._data.append(item)

    def get_all(self):
        with self._lock:
            if self._ttl == 0:
                return list(self._data)
            cutoff = time.time() - self._ttl
            return [item for ts, item in self._data if ts > cutoff]

    def get_recent(self, count: int):
        with self._lock:
            items = self.get_all()
            return items[-count:]

    def __len__(self):
        with self._lock:
            return len(self._data)

    def __iter__(self):
        with self._lock:
            return iter(self.get_all())

    def clear(self):
        with self._lock:
            self._data.clear()

    @property
    def maxlen(self):
        return self._maxlen

