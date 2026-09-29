from __future__ import annotations

import logging
import time
import re
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    content: str
    score: float
    source: str = ""
    signals: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"content": self.content[:200], "score": round(self.score, 3),
                "source": self.source, "signals": self.signals}


class MultiSignalRetrieval:
    def __init__(self, vector_search_fn: Callable | None = None,
                 keyword_search_fn: Callable | None = None,
                 entity_extractor: Callable | None = None):
        self._vector_search_fn = vector_search_fn
        self._keyword_search_fn = keyword_search_fn
        self._entity_extractor = entity_extractor

    async def search(self, query: str, top_k: int = 10) -> list[RetrievalResult]:
        results: dict[str, RetrievalResult] = {}

        # Signal 1: Semantic (vector)
        if self._vector_search_fn:
            try:
                vector_results = await self._vector_search_fn(query, top_k=top_k)
                for item in vector_results:
                    content = item if isinstance(item, str) else (item.get("content", "") if isinstance(item, dict) else str(item))
                    score = 1.0 if isinstance(item, str) else (item.get("score", 0.8) if isinstance(item, dict) else 0.8)
                    source = item.get("source", "vector") if isinstance(item, dict) else "vector"
                    if content not in results or results[content].score < score:
                        results[content] = RetrievalResult(
                            content=content, score=score, source=source, signals=["vector"],
                        )
            except Exception as e:
                logger.debug("Vector search failed: %s", e)

        # Signal 2: Keyword (FTS5 / BM25)
        if self._keyword_search_fn:
            try:
                kw_results = self._keyword_search_fn(query, top_k=top_k)
                for item in kw_results:
                    content = item if isinstance(item, str) else (item.get("content", "") if isinstance(item, dict) else str(item))
                    score = 0.7 if isinstance(item, str) else (item.get("score", 0.7) if isinstance(item, dict) else 0.7)
                    source = item.get("source", "keyword") if isinstance(item, dict) else "keyword"
                    if content in results:
                        results[content].score += score * 0.3
                        results[content].signals.append("keyword")
                        results[content].score = min(results[content].score, 1.0)
                    else:
                        results[content] = RetrievalResult(
                            content=content, score=score * 0.7, source=source, signals=["keyword"],
                        )
            except Exception as e:
                logger.debug("Keyword search failed: %s", e)

        # Signal 3: Entity matching
        if self._entity_extractor:
            try:
                entities = self._entity_extractor(query)
                for entity in entities:
                    entity_lower = entity.lower()
                    for content, result in list(results.items()):
                        if entity_lower in content.lower():
                            result.score += 0.15
                            if "entity" not in result.signals:
                                result.signals.append("entity")
            except Exception as e:
                logger.debug("Entity extraction failed: %s", e)

        # Recency boost
        for result in results.values():
            if "recency" in result.metadata:
                hours_old = (time.time() - result.metadata["recency"]) / 3600.0
                recency_boost = max(0, 1.0 - hours_old / 168.0) * 0.1
                result.score += recency_boost

        sorted_results = sorted(results.values(), key=lambda r: r.score, reverse=True)
        return sorted_results[:top_k]

    @staticmethod
    def extract_entities(text: str) -> list[str]:
        entities = []
        # Capitalized multi-word phrases (potential named entities)
        for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text):
            entities.append(match.group(1))
        # Common entity patterns: emails, URLs, code identifiers
        for match in re.finditer(r'\b[\w.-]+@[\w.-]+\.\w+\b', text):
            entities.append(match.group(0))
        for match in re.finditer(r'\b[A-Z][a-z]+(?:_[A-Z][a-z]+)+\b', text):
            entities.append(match.group(0))
        return list(set(entities))

    @staticmethod
    def keyword_search(text: str, query: str) -> float:
        text_lower = text.lower()
        query_lower = query.lower()
        query_words = set(re.findall(r'\w{3,}', query_lower))
        if not query_words:
            return 0.0
        matches = sum(1 for w in query_words if w in text_lower)
        return matches / len(query_words)
