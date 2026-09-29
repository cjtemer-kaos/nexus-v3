import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("nexus-waitfor")


@dataclass
class WaitResult:
    success: bool = False
    reason: str = ""
    duration_ms: float = 0.0
    dom_stable: bool = False
    navigations: int = 0


class WaitForHelper:
    def __init__(self, dom_stability_timeout: float = 1.0,
                 max_wait: float = 10.0, mutation_timeout: float = 5.0):
        self.dom_stability_timeout = dom_stability_timeout
        self.max_wait = max_wait
        self.mutation_timeout = mutation_timeout

    async def wait_for_navigation(self, get_url: Callable, timeout: float = 0) -> WaitResult:
        start = time.time()
        deadline = time.time() + (timeout or self.max_wait)
        initial_url = get_url() if callable(get_url) else ""
        navigations = 0
        while time.time() < deadline:
            current = get_url() if callable(get_url) else ""
            if current and current != initial_url:
                navigations += 1
                await asyncio.sleep(0.5)
                return WaitResult(
                    success=True,
                    reason="Navigation detected",
                    duration_ms=(time.time() - start) * 1000,
                    navigations=navigations,
                )
            await asyncio.sleep(0.1)
        return WaitResult(
            success=bool(initial_url),
            reason="Navigation timeout",
            duration_ms=(time.time() - start) * 1000,
            navigations=navigations,
        )

    async def wait_for_dom_stability(self, evaluate_stability: Callable, timeout: float = 0) -> WaitResult:
        start = time.time()
        deadline = time.time() + (timeout or self.mutation_timeout)
        stable_since: Optional[float] = None
        while time.time() < deadline:
            try:
                is_stable = evaluate_stability() if callable(evaluate_stability) else True
                if is_stable:
                    if stable_since is None:
                        stable_since = time.time()
                    elif time.time() - stable_since >= self.dom_stability_timeout:
                        return WaitResult(
                            success=True,
                            reason="DOM stable",
                            duration_ms=(time.time() - start) * 1000,
                            dom_stable=True,
                        )
                else:
                    stable_since = None
            except Exception:
                stable_since = None
            await asyncio.sleep(0.1)
        return WaitResult(
            success=False,
            reason="DOM stability timeout",
            duration_ms=(time.time() - start) * 1000,
            dom_stable=stable_since is not None,
        )

    async def _backoff_sleep(self, attempt: int, base: float = 0.1):
        delay = min(base * (2 ** attempt), 2.0)
        import random
        await asyncio.sleep(delay + random.uniform(0, delay * 0.1))

    async def wait_for_selector(self, selector: str, evaluate_script: Callable,
                                 timeout: float = 0) -> WaitResult:
        import json
        start = time.time()
        deadline = time.time() + (timeout or self.max_wait)
        safe_selector = json.dumps(selector)
        script = f"document.querySelector({safe_selector}) !== null"
        attempt = 0
        while time.time() < deadline:
            try:
                exists = evaluate_script(script)
                if exists:
                    return WaitResult(
                        success=True,
                        reason=f"Selector found: {selector}",
                        duration_ms=(time.time() - start) * 1000,
                    )
            except (TypeError, ValueError, RuntimeError) as e:
                logger.debug(f"Selector eval error: {e}")
            await self._backoff_sleep(attempt)
            attempt += 1
        return WaitResult(
            success=False,
            reason=f"Selector not found: {selector}",
            duration_ms=(time.time() - start) * 1000,
        )

    async def wait_for_condition(self, condition_fn: Callable, timeout: float = 0,
                                  poll_interval: float = 0.1) -> WaitResult:
        start = time.time()
        deadline = time.time() + (timeout or self.max_wait)
        attempt = 0
        while time.time() < deadline:
            try:
                if condition_fn():
                    return WaitResult(
                        success=True,
                        reason="Condition met",
                        duration_ms=(time.time() - start) * 1000,
                    )
            except (TypeError, ValueError, RuntimeError) as e:
                logger.debug(f"Condition eval error: {e}")
            await self._backoff_sleep(attempt, poll_interval)
            attempt += 1
        return WaitResult(
            success=False,
            reason="Condition not met within timeout",
            duration_ms=(time.time() - start) * 1000,
        )

    async def wait_after_action(self, get_url: Callable, evaluate_stability: Callable) -> WaitResult:
        nav = await self.wait_for_navigation(get_url, timeout=3.0)
        stab = await self.wait_for_dom_stability(evaluate_stability, timeout=3.0)
        return WaitResult(
            success=nav.success or stab.success,
            reason=f"nav={'yes' if nav.success else 'no'}, dom={'stable' if stab.dom_stable else 'unstable'}",
            duration_ms=nav.duration_ms + stab.duration_ms,
            dom_stable=stab.dom_stable,
            navigations=nav.navigations,
        )
