"""
ContextManager — Intelligent Context Manager (Cursor-style)

Features:
  - In-memory context store with relevance scoring, TTL, and access tracking
  - Relevance = recency × access_count × relevance (weighted)
  - Automatic TTL-based expiration of stale items
  - Token-aware compression with bytes/4 heuristic
  - Prompt injection: prepend relevant context up to max_tokens budget
  - Singleton via get_context_manager()

Scoring formula:
  score = relevance × (1 + log2(1 + access_count)) × recency_factor
  recency_factor = 1.0 / (1 + hours_since_creation / 24)

Patrons:
  - Dataclass models
  - Thread-safe via threading.Lock
  - No external dependencies (pure stdlib)
"""

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-context-manager")


# ─── Dataclasses ──────────────────────────────────────────────────────

@dataclass
class ContextItem:
    id: str
    content: str
    source: str
    relevance: float = 0.5
    created_at: float = 0.0
    accessed_at: float = 0.0
    access_count: int = 0
    ttl_seconds: Optional[int] = None


@dataclass
class ContextWindow:
    total_tokens: int = 0
    items: List[ContextItem] = field(default_factory=list)
    truncated: bool = False


# ─── Helpers ──────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Estimate tokens using bytes/4 heuristic."""
    return len(text.encode("utf-8")) // 4 if text else 0


# ─── Singleton ────────────────────────────────────────────────────────

_manager_singleton: Optional["ContextManager"] = None
_manager_lock = threading.Lock()


def get_context_manager() -> "ContextManager":
    global _manager_singleton
    if _manager_singleton is None:
        with _manager_lock:
            if _manager_singleton is None:
                _manager_singleton = ContextManager()
    return _manager_singleton


# ─── Core class ───────────────────────────────────────────────────────

class ContextManager:
    """Intelligent context management with relevance scoring and TTL."""

    def __init__(self):
        self._items: Dict[str, ContextItem] = {}
        self._lock = threading.Lock()
        self._total_injections: int = 0
        self._total_tokens_injected: int = 0

    # ── Public API ────────────────────────────────────────────────────

    def add_context(
        self,
        context_id: str,
        content: str,
        source: str,
        relevance: float = 0.5,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        """Add or update a context item."""
        now = time.time()
        with self._lock:
            existing = self._items.get(context_id)
            if existing:
                existing.content = content
                existing.source = source
                existing.relevance = relevance
                existing.accessed_at = now
                existing.ttl_seconds = ttl_seconds
                logger.debug("Context updated: %s", context_id)
            else:
                self._items[context_id] = ContextItem(
                    id=context_id,
                    content=content,
                    source=source,
                    relevance=relevance,
                    created_at=now,
                    accessed_at=now,
                    access_count=0,
                    ttl_seconds=ttl_seconds,
                )
                logger.debug("Context added: %s (source=%s)", context_id, source)

    def get_relevant_context(
        self, query: str, limit: int = 10
    ) -> List[ContextItem]:
        """
        Get context items ranked by composite score:
          score = relevance × (1 + log2(1 + access_count)) × recency_factor
        Filter out TTL-expired items. Touch accessed_at + access_count.
        """
        now = time.time()
        query_lower = query.lower()
        query_words = set(query_lower.split())
        scored: List[tuple[float, ContextItem]] = []

        with self._lock:
            for item in self._items.values():
                # TTL check
                if item.ttl_seconds and (now - item.created_at) > item.ttl_seconds:
                    continue

                # Simple keyword overlap boosting
                content_lower = item.content.lower()
                keyword_bonus = 1.0
                if query_words:
                    matches = sum(1 for w in query_words if w in content_lower)
                    keyword_bonus = 1.0 + (matches / len(query_words)) * 0.5

                # Recency factor: halve every 24h
                hours = (now - item.created_at) / 3600.0
                recency = 1.0 / (1.0 + hours / 24.0)

                # Access frequency bonus (log-scaled)
                access_bonus = 1.0 + math.log2(1 + item.access_count)

                score = item.relevance * access_bonus * recency * keyword_bonus

                # Touch access stats
                item.accessed_at = now
                item.access_count += 1

                scored.append((score, item))

        # Sort descending by score
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:limit]]

    def compress_context(self, max_tokens: int = 4000) -> str:
        """
        Compress all context into a single string:
          1. Remove TTL-expired items
          2. Sort by composite score
          3. Truncate content if over max_tokens budget
        Returns the compressed string.
        """
        now = time.time()
        active: List[ContextItem] = []

        with self._lock:
            for item in self._items.values():
                if item.ttl_seconds and (now - item.created_at) > item.ttl_seconds:
                    continue
                active.append(item)

        # Score and sort
        scored = []
        for item in active:
            hours = (now - item.created_at) / 3600.0
            recency = 1.0 / (1.0 + hours / 24.0)
            access_bonus = 1.0 + math.log2(1 + item.access_count)
            score = item.relevance * access_bonus * recency
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Build compressed string within token budget
        parts: List[str] = []
        remaining = max_tokens

        for score, item in scored:
            item_tokens = estimate_tokens(item.content)
            if item_tokens <= remaining:
                parts.append(f"[{item.source}] {item.content}")
                remaining -= item_tokens
            elif remaining > 10:
                # Truncate to fit
                truncated = item.content[:remaining * 4]  # 4 bytes/token
                parts.append(f"[{item.source}] {truncated}...")
                remaining = 0
            else:
                break

        return "\n\n".join(parts)

    def get_context_window(self) -> ContextWindow:
        """Get current context window with token estimate."""
        now = time.time()
        items: List[ContextItem] = []
        total = 0

        with self._lock:
            for item in self._items.values():
                if item.ttl_seconds and (now - item.created_at) > item.ttl_seconds:
                    continue
                items.append(item)
                total += estimate_tokens(item.content)

        return ContextWindow(
            total_tokens=total,
            items=items,
            truncated=False,
        )

    def inject_context(self, prompt: str, max_tokens: int = 4000) -> str:
        """
        Prepend relevant context to a prompt, respecting token budget.
        Returns: context_block + prompt (if context fits) or just prompt.
        """
        prompt_tokens = estimate_tokens(prompt)
        budget = max_tokens - prompt_tokens

        if budget <= 0:
            logger.debug("Prompt alone exceeds max_tokens, no context injected")
            return prompt

        context_str = self.compress_context(max_tokens=budget)

        self._total_injections += 1
        self._total_tokens_injected += estimate_tokens(context_str)

        if context_str.strip():
            return f"=== CONTEXT ===\n{context_str}\n=== END CONTEXT ===\n\n{prompt}"
        return prompt

    def remove_context(self, context_id: str) -> bool:
        """Remove a context item. Returns True if it existed."""
        with self._lock:
            if context_id in self._items:
                del self._items[context_id]
                logger.debug("Context removed: %s", context_id)
                return True
            return False

    def get_stats(self) -> Dict[str, Any]:
        """Get context manager statistics."""
        now = time.time()
        active = 0
        expired = 0
        total_tokens = 0
        sources: Dict[str, int] = {}

        with self._lock:
            for item in self._items.values():
                if item.ttl_seconds and (now - item.created_at) > item.ttl_seconds:
                    expired += 1
                else:
                    active += 1
                    total_tokens += estimate_tokens(item.content)
                    sources[item.source] = sources.get(item.source, 0) + 1

        return {
            "total_items": active + expired,
            "active_items": active,
            "expired_items": expired,
            "total_estimated_tokens": total_tokens,
            "total_injections": self._total_injections,
            "total_tokens_injected": self._total_tokens_injected,
            "sources": sources,
        }


# ─── Module-level convenience ─────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    mgr = get_context_manager()

    # Add sample context items
    mgr.add_context("auth-spec", "OAuth2 flow: authorize → code → token → refresh",
                     "architecture", relevance=0.9, ttl_seconds=3600)
    mgr.add_context("db-schema", "Users table: id, name, email, created_at",
                     "database", relevance=0.7)
    mgr.add_context("api-routes", "POST /users, GET /users/:id, PUT /users/:id",
                     "api-spec", relevance=0.8)
    mgr.add_context("error-handling", "All errors wrapped in ErrorEnvelope",
                     "conventions", relevance=0.6, ttl_seconds=7200)

    # Query
    results = mgr.get_relevant_context("user authentication API", limit=3)
    print("Relevant context:")
    for item in results:
        print(f"  [{item.source}] {item.content[:60]}... (accessed {item.access_count}x)")

    # Inject into a prompt
    prompt = "Write a new endpoint for user registration"
    injected = mgr.inject_context(prompt, max_tokens=2000)
    print(f"\nInjected prompt ({estimate_tokens(injected)} tokens):")
    print(injected[:300], "...")

    # Stats
    print(f"\nStats: {mgr.get_stats()}")
