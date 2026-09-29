"""
Credential Pool - Persistent multi-credential and multi-model failover.

Adapted for SuperNEXUS v2.0 (Ollama + Cloud fallback)
"""

import logging
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ProviderType(Enum):
    OLLAMA = "ollama"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENROUTER = "openrouter"
    CUSTOM = "custom"


@dataclass
class ProviderHealth:
    name: str
    provider_type: ProviderType
    rate_limited_until: float = 0.0
    consecutive_errors: int = 0
    total_requests: int = 0
    total_successes: int = 0
    last_success: float = 0.0
    last_error: str = ""


@dataclass
class ModelInfo:
    name: str
    provider: str
    context_length: int = 4096
    supports_vision: bool = False
    priority: int = 0


class FailoverStrategy(Enum):
    FILL_FIRST = "fill_first"
    ROUND_ROBIN = "round_robin"
    RANDOM = "random"
    LEAST_USED = "least_used"


STATUS_OK = "ok"
STATUS_EXHAUSTED = "exhausted"
EXHAUSTED_TTL_401_SECONDS = 5 * 60
EXHAUSTED_TTL_429_SECONDS = 60 * 60
EXHAUSTED_TTL_DEFAULT_SECONDS = 60 * 60


class CredentialPool:
    """Multi-provider LLM failover pool with health tracking."""

    def __init__(self, primary_provider: str = "ollama", strategy: FailoverStrategy = FailoverStrategy.FILL_FIRST):
        self._primary = primary_provider
        self._strategy = strategy
        self._providers: Dict[str, ProviderHealth] = {}
        self._models: Dict[str, List[ModelInfo]] = {}
        self._index = 0
        self._lock = threading.Lock()
        
        self.register_provider("ollama", ProviderType.OLLAMA, "http://localhost:11434")
        self.register_model("ollama", ModelInfo(name="qwen2.5-coder:7b", provider="ollama", context_length=4096, priority=10))
        self.register_model("ollama", ModelInfo(name="deepseek-r1:8b", provider="ollama", context_length=4096, priority=8))
        self.register_model("ollama", ModelInfo(name="qwen2.5vl:2b", provider="ollama", context_length=4096, supports_vision=True, priority=5))

    def register_provider(self, name: str, provider_type: ProviderType, endpoint: str = None, api_key: str = None):
        with self._lock:
            self._providers[name] = ProviderHealth(name=name, provider_type=provider_type)
            logger.info(f"[credential-pool] Registered provider: {name} ({provider_type.value})")

    def register_model(self, provider: str, model: ModelInfo):
        with self._lock:
            if provider not in self._models:
                self._models[provider] = []
            self._models[provider].append(model)
            self._models[provider].sort(key=lambda m: m.priority, reverse=True)

    def get_provider(self, model: str = None) -> Optional[str]:
        with self._lock:
            now = time.monotonic()
            candidates = []
            
            if model and model in self._models:
                for provider_name in self._models:
                    if any(m.name == model for m in self._models[provider_name]):
                        candidates.append(provider_name)
            else:
                candidates = list(self._providers.keys())
            
            if not candidates:
                return self._primary
            
            if self._strategy == FailoverStrategy.ROUND_ROBIN:
                return self._round_robin_selection(candidates, now)
            elif self._strategy == FailoverStrategy.LEAST_USED:
                return self._least_used_selection(candidates, now)
            elif self._strategy == FailoverStrategy.RANDOM:
                return self._random_selection(candidates, now)
            else:
                return self._fill_first_selection(candidates, now)

    def _round_robin_selection(self, candidates: List[str], now: float) -> str:
        for _ in range(len(candidates)):
            provider = candidates[self._index % len(candidates)]
            self._index += 1
            if self._is_healthy(provider, now):
                return provider
        return self._primary if self._is_healthy(self._primary, now) else candidates[0]

    def _fill_first_selection(self, candidates: List[str], now: float) -> str:
        if self._primary in candidates and self._is_healthy(self._primary, now):
            return self._primary
        for provider in candidates:
            if self._is_healthy(provider, now):
                return provider
        return self._primary if self._is_healthy(self._primary, now) else candidates[0]

    def _least_used_selection(self, candidates: List[str], now: float) -> str:
        best = None
        best_requests = float('inf')
        for provider in candidates:
            if self._is_healthy(provider, now):
                requests = self._providers[provider].total_requests
                if requests < best_requests:
                    best = provider
                    best_requests = requests
        return best or self._primary

    def _random_selection(self, candidates: List[str], now: float) -> str:
        healthy = [p for p in candidates if self._is_healthy(p, now)]
        if healthy:
            return random.choice(healthy)
        return self._primary

    def _is_healthy(self, provider: str, now: float) -> bool:
        if provider not in self._providers:
            return False
        return self._providers[provider].rate_limited_until <= now

    def mark_success(self, provider: str):
        with self._lock:
            if provider in self._providers:
                health = self._providers[provider]
                health.consecutive_errors = 0
                health.total_successes += 1
                health.total_requests += 1
                health.last_success = time.monotonic()
                health.rate_limited_until = 0.0

    def mark_error(self, provider: str, error: str, retry_after: float = None):
        with self._lock:
            if provider not in self._providers:
                return
            health = self._providers[provider]
            health.consecutive_errors += 1
            health.total_requests += 1
            health.last_error = error
            
            if retry_after:
                cooldown = retry_after
            elif "401" in error or "403" in error:
                cooldown = EXHAUSTED_TTL_401_SECONDS
            elif "429" in error:
                cooldown = EXHAUSTED_TTL_429_SECONDS
            else:
                cooldown = EXHAUSTED_TTL_DEFAULT_SECONDS
            
            health.rate_limited_until = time.monotonic() + cooldown

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            status = {"primary": self._primary, "strategy": self._strategy.value, "providers": {}}
            for name, health in self._providers.items():
                status["providers"][name] = {
                    "healthy": self._is_healthy(name, now),
                    "requests": health.total_requests,
                    "successes": health.total_successes,
                    "consecutive_errors": health.consecutive_errors,
                    "cooldown_remaining": max(0, health.rate_limited_until - now),
                }
            return status


_pool: Optional[CredentialPool] = None
_pool_lock = threading.Lock()


def get_credential_pool() -> CredentialPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = CredentialPool(primary_provider="ollama", strategy=FailoverStrategy.FILL_FIRST)
            logger.info("[credential-pool] Initialized global pool")
        return _pool


def reset_credential_pool():
    global _pool
    with _pool_lock:
        _pool = None