"""
LLM Gateway - Unified LLM access with fallback chain

Provides a single interface to multiple LLM providers:
1. Ollama (local, free, primary)
2. OpenRouter (cloud, fallback)
3. FreeQwenApi (cloud, free fallback)
4. PC2 Ollama (remote local, fallback)

Features:
- Automatic fallback chain
- Circuit breaker per provider
- Latency tracking
- Cost tracking
- Provider health monitoring
- Smart routing by task type
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

import httpx

logger = logging.getLogger("nexus-llm-gateway")


class ProviderStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str = ""
    models: List[str] = field(default_factory=list)
    timeout: float = 60.0
    max_retries: int = 2
    priority: int = 0  # Lower = higher priority
    enabled: bool = True
    cost_per_1m_tokens: float = 0.0  # 0 for local
    api_path: str = "/api/chat"  # Ollama; OpenAI-compatible usa /v1/chat/completions o /chat/completions


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 60.0
    failure_count: int = 0
    last_failure_time: float = 0.0
    state: str = "closed"  # closed, open, half-open

    def record_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(f"Circuit breaker OPEN (failures: {self.failure_count})")

    def record_success(self):
        self.failure_count = 0
        self.state = "closed"

    def can_execute(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half-open"
                logger.info("Circuit breaker HALF-OPEN (testing recovery)")
                return True
            return False
        return True  # half-open


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    tokens_used: int = 0
    duration_ms: float = 0.0
    cost: float = 0.0
    fallback_chain: List[str] = field(default_factory=list)


class LLMGateway:
    """
    Unified LLM gateway with automatic fallback chain.

    Usage:
        gateway = LLMGateway()
        gateway.add_provider("ollama", "http://localhost:11434", priority=0)
        gateway.add_provider("pc2", os.environ.get("PC2_OLLAMA_URL", "http://remote-host:11434"), priority=1)
        response = await gateway.chat("What is Python?", model="qwen2.5-coder:7b")
    """

    def __init__(self):
        self._providers: Dict[str, ProviderConfig] = {}
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._stats: Dict[str, Dict] = {}
        self._total_tokens = 0
        self._total_cost = 0.0
        self._request_count = 0

    def add_provider(
        self,
        name: str,
        base_url: str,
        api_key: str = "",
        models: List[str] = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        priority: int = 0,
        cost_per_1m_tokens: float = 0.0,
        api_path: str = "/api/chat",
    ) -> 'LLMGateway':
        """Add a provider to the gateway."""
        config = ProviderConfig(
            name=name,
            base_url=base_url,
            api_key=api_key,
            models=models or [],
            timeout=timeout,
            max_retries=max_retries,
            priority=priority,
            cost_per_1m_tokens=cost_per_1m_tokens,
            api_path=api_path,
        )
        self._providers[name] = config
        self._circuit_breakers[name] = CircuitBreaker()
        self._stats[name] = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "total_tokens": 0,
            "avg_latency_ms": 0.0,
            "last_used": None,
        }
        logger.info(f"Provider added: {name} (priority: {priority}, url: {base_url}, path: {api_path})")
        return self

    def remove_provider(self, name: str) -> bool:
        """Remove a provider."""
        if name in self._providers:
            del self._providers[name]
            del self._circuit_breakers[name]
            del self._stats[name]
            logger.info(f"Provider removed: {name}")
            return True
        return False

    def disable_provider(self, name: str):
        """Disable a provider without removing it."""
        if name in self._providers:
            self._providers[name].enabled = False
            logger.info(f"Provider disabled: {name}")

    def enable_provider(self, name: str):
        """Enable a disabled provider."""
        if name in self._providers:
            self._providers[name].enabled = True
            self._circuit_breakers[name].state = "closed"
            self._circuit_breakers[name].failure_count = 0
            logger.info(f"Provider enabled: {name}")

    def _get_ordered_providers(self) -> List[ProviderConfig]:
        """Get providers ordered by priority (lowest first), excluding disabled."""
        enabled = [p for p in self._providers.values() if p.enabled]
        return sorted(enabled, key=lambda p: p.priority)

    async def chat(
        self,
        messages: List[Dict[str, str]],
        model: str = "qwen2.5-coder:7b",
        options: Dict = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        """
        Chat with LLM, trying providers in priority order with automatic fallback.

        Args:
            messages: List of message dicts
            model: Model name to use
            options: Additional model options
            temperature: Temperature for generation
            max_tokens: Maximum tokens to generate

        Returns:
            LLMResponse with content and metadata
        """
        self._request_count += 1
        providers = self._get_ordered_providers()
        fallback_chain = []
        last_error = None

        for provider in providers:
            cb = self._circuit_breakers[provider.name]

            if not cb.can_execute():
                logger.debug(f"Provider {provider.name} circuit breaker open, skipping")
                fallback_chain.append(f"{provider.name}(circuit_open)")
                continue

            start = time.time()
            try:
                result = await self._call_provider(
                    provider=provider,
                    messages=messages,
                    model=model,
                    options=options,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )

                duration_ms = (time.time() - start) * 1000
                cb.record_success()

                # Update stats
                stats = self._stats[provider.name]
                stats["requests"] += 1
                stats["successes"] += 1
                stats["total_tokens"] += result.get("tokens_used", 0)
                stats["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")

                # Smooth average latency
                old_avg = stats["avg_latency_ms"]
                stats["avg_latency_ms"] = (old_avg * 0.9) + (duration_ms * 0.1)

                self._total_tokens += result.get("tokens_used", 0)
                cost = (result.get("tokens_used", 0) / 1_000_000) * provider.cost_per_1m_tokens
                self._total_cost += cost

                return LLMResponse(
                    content=result.get("content", ""),
                    provider=provider.name,
                    model=result.get("model", model),
                    tokens_used=result.get("tokens_used", 0),
                    duration_ms=duration_ms,
                    cost=cost,
                    fallback_chain=fallback_chain,
                )

            except Exception as e:
                duration_ms = (time.time() - start) * 1000
                cb.record_failure()
                last_error = e

                stats = self._stats[provider.name]
                stats["requests"] += 1
                stats["failures"] += 1

                fallback_chain.append(f"{provider.name}(error:{str(e)[:50]})")
                logger.warning(f"Provider {provider.name} failed: {e} ({duration_ms:.0f}ms)")

        # All providers failed
        return LLMResponse(
            content=f"All LLM providers failed. Last error: {last_error}",
            provider="none",
            model=model,
            duration_ms=0,
            fallback_chain=fallback_chain,
        )

    async def _call_provider(
        self,
        provider: ProviderConfig,
        messages: List[Dict[str, str]],
        model: str,
        options: Dict = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> Dict:
        """Call a specific provider's API (Ollama o OpenAI-compatible según api_path)."""
        async with httpx.AsyncClient(timeout=provider.timeout) as client:
            if provider.api_path == "/api/chat":
                # Formato Ollama
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                        "num_predict": max_tokens,
                        **(options or {}),
                    },
                }
            else:
                # Formato OpenAI-compatible (llama.cpp / zen)
                payload = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if options:
                    payload.update(options)

            headers = {}
            if provider.api_key:
                headers["Authorization"] = f"Bearer {provider.api_key}"

            url = f"{provider.base_url}{provider.api_path}"

            response = await client.post(
                url,
                json=payload,
                headers=headers,
            )
            # Retry on 502 (model loading — transient)
            if response.status_code == 502:
                logger.warning(f"Provider {provider.name} 502 for {model}, retrying in 3s...")
                await asyncio.sleep(3)
                response = await client.post(
                    url,
                    json=payload,
                    headers=headers,
                )
            response.raise_for_status()

            data = response.json()
            if provider.api_path == "/api/chat":
                content = data.get("message", {}).get("content", "")
                tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)
                resp_model = data.get("model", model)
            else:
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = msg.get("content", "")
                usage = data.get("usage") or {}
                tokens = usage.get("total_tokens", 0)
                resp_model = data.get("model", model)

            return {
                "content": content,
                "model": resp_model,
                "tokens_used": tokens,
            }

    async def chat_simple(
        self,
        prompt: str,
        model: str = "qwen2.5-coder:7b",
        system_prompt: str = "",
        **kwargs,
    ) -> LLMResponse:
        """Simple chat with just a prompt string."""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        return await self.chat(messages, model=model, **kwargs)

    def get_provider_status(self) -> Dict[str, Dict]:
        """Get status of all providers."""
        status = {}
        for name, provider in self._providers.items():
            cb = self._circuit_breakers[name]
            stats = self._stats[name]

            if cb.state == "open":
                provider_status = ProviderStatus.UNHEALTHY.value
            elif cb.failure_count > 0:
                provider_status = ProviderStatus.DEGRADED.value
            elif not provider.enabled:
                provider_status = ProviderStatus.DISABLED.value
            else:
                provider_status = ProviderStatus.HEALTHY.value

            status[name] = {
                "status": provider_status,
                "priority": provider.priority,
                "url": provider.base_url,
                "circuit_breaker": cb.state,
                "failure_count": cb.failure_count,
                "requests": stats["requests"],
                "successes": stats["successes"],
                "failures": stats["failures"],
                "avg_latency_ms": round(stats["avg_latency_ms"], 1),
                "last_used": stats["last_used"],
            }
        return status

    def get_stats(self) -> Dict:
        """Get gateway-wide statistics."""
        return {
            "total_requests": self._request_count,
            "total_tokens": self._total_tokens,
            "total_cost": round(self._total_cost, 4),
            "providers": len(self._providers),
            "enabled_providers": sum(1 for p in self._providers.values() if p.enabled),
            "provider_status": self.get_provider_status(),
        }

    def reset_stats(self):
        """Reset all statistics."""
        self._total_tokens = 0
        self._total_cost = 0.0
        self._request_count = 0
        for stats in self._stats.values():
            stats["requests"] = 0
            stats["successes"] = 0
            stats["failures"] = 0
            stats["total_tokens"] = 0
            stats["avg_latency_ms"] = 0.0
        for cb in self._circuit_breakers.values():
            cb.failure_count = 0
            cb.state = "closed"
