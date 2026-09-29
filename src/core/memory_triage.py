# src/core/memory_triage.py
"""
4-Gate Memory Triage — Filter before storage.

Every memory candidate passes 4 gates:
1. Future Utility — will this be useful later? (keyword density + length)
2. Novelty — is this new or already known? (dedup via normalized hash)
3. Factual Accuracy — is this stated with confidence? (hedge word detection)
4. Safety — does it contain PII, secrets, or sensitive data? (regex patterns)

Only memories passing all 4 gates get persisted.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class TriageGate(str, Enum):
    FUTURE_UTILITY = "future_utility"
    NOVELTY = "novelty"
    FACTUAL = "factual"
    SAFETY = "safety"


@dataclass
class TriageResult:
    content: str
    passed: bool
    score: float = 0.0  # 0-10
    rejected_by: TriageGate | None = None
    factual_confidence: float = 1.0
    gates_passed: list[str] = field(default_factory=list)
    reason: str = ""


# --- Gate 1: Future Utility ---

TRIVIAL_PATTERNS = [
    re.compile(r"^(ok|okay|sure|yes|no|right|got\s*it|thanks?|thx|bye|hi|hey|hello)\b", re.I),
    re.compile(r"^(good|great|nice|cool|awesome|perfect|fine)\b", re.I),
    re.compile(r"^\W*$"),  # empty/whitespace
]

VALUABLE_KEYWORDS = {
    "pattern", "architecture", "algorithm", "protocol", "implementation",
    "design", "strategy", "optimization", "security", "performance",
    "database", "cache", "queue", "pipeline", "middleware", "framework",
    "api", "endpoint", "schema", "migration", "deploy", "docker",
    "asyncio", "concurrent", "parallel", "distributed", "resilience",
    "circuit", "breaker", "retry", "timeout", "backoff", "fallback",
}


def _future_utility_score(text: str) -> float:
    """Score 0-10 based on information density and length."""
    for pat in TRIVIAL_PATTERNS:
        if pat.match(text.strip()):
            return 0.0

    words = re.findall(r'\w+', text.lower())
    if len(words) < 5:
        return 1.0

    valuable_count = sum(1 for w in words if w in VALUABLE_KEYWORDS)
    density = valuable_count / max(len(words), 1)

    length_bonus = min(len(words) / 20.0, 2.0)

    code_bonus = 1.0 if any(c in text for c in ["()", "->", "=>", "def ", "class ", "{}", "```"]) else 0.0

    score = (density * 6.0) + length_bonus + code_bonus
    return min(score, 10.0)


# --- Gate 2: Novelty ---

def _normalize_for_dedup(text: str) -> str:
    """Normalize text for duplicate detection."""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text


def _content_hash(text: str) -> str:
    normalized = _normalize_for_dedup(text)
    return hashlib.md5(normalized.encode()).hexdigest()


# --- Gate 3: Factual Accuracy ---

HEDGE_WORDS = {
    "maybe", "perhaps", "possibly", "might", "could", "probably",
    "i think", "i guess", "not sure", "i believe", "supposedly",
    "apparently", "seems like", "it appears", "allegedly",
}


def _factual_confidence(text: str) -> float:
    """Score 0-1 based on hedge word density. More hedging = lower confidence."""
    text_lower = text.lower()
    hedge_count = sum(1 for hw in HEDGE_WORDS if hw in text_lower)
    words = len(re.findall(r'\w+', text_lower))
    if words < 3:
        return 0.5
    hedge_ratio = hedge_count / max(words / 5.0, 1.0)
    return max(0.0, 1.0 - hedge_ratio * 0.5)


# --- Gate 4: Safety ---

SAFETY_PATTERNS = [
    re.compile(r'(?:api[_\s-]?key|secret[_\s-]?key|token)\s*[:=]\s*\S+', re.I),
    re.compile(r'sk-[a-zA-Z0-9]{10,}'),  # OpenAI-style keys
    re.compile(r'password\s*[:=]\s*\S+', re.I),
    re.compile(r'password\s+is\s+\S+', re.I),
    re.compile(r'(?:api[_\s-]?key|secret[_\s-]?key|token)\s+is\s+\S+', re.I),
    re.compile(r'\b\d{3}[-.\s]?\d{2}[-.\s]?\d{4}\b'),  # SSN
    re.compile(r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b'),  # credit cards
    re.compile(r'\b4\d{3}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b'),  # Visa with separators
    re.compile(r'(?:BEGIN|END)\s+(?:RSA|DSA|EC|OPENSSH)\s+(?:PRIVATE|PUBLIC)\s+KEY', re.I),
]


def _safety_check(text: str) -> bool:
    """Returns True if text is SAFE (no PII/secrets detected)."""
    for pat in SAFETY_PATTERNS:
        if pat.search(text):
            return False
    return True


# --- Main Triage ---

class MemoryTriage:
    """
    4-Gate Memory Triage.

    Usage:
        triage = MemoryTriage()
        result = triage.evaluate("some content to memorize")
        if result.passed:
            memory.store(result.content)
    """

    def __init__(self, min_utility: float = 2.0, min_factual: float = 0.3):
        self._min_utility = min_utility
        self._min_factual = min_factual
        self._known_hashes: set[str] = set()
        self._stats = {"evaluated": 0, "passed": 0, "rejected": {g.value: 0 for g in TriageGate}}

    def register_known(self, *contents: str) -> None:
        """Register existing memory content for novelty dedup."""
        for content in contents:
            self._known_hashes.add(_content_hash(content))

    def evaluate(self, content: str) -> TriageResult:
        """Run content through all 4 gates."""
        self._stats["evaluated"] += 1
        gates_passed = []

        # Gate 1: Future Utility
        utility = _future_utility_score(content)
        if utility < self._min_utility:
            self._stats["rejected"]["future_utility"] += 1
            return TriageResult(
                content=content, passed=False, score=utility,
                rejected_by=TriageGate.FUTURE_UTILITY,
                reason=f"Low utility score: {utility:.1f}",
            )
        gates_passed.append("future_utility")

        # Gate 2: Novelty
        h = _content_hash(content)
        if h in self._known_hashes:
            self._stats["rejected"]["novelty"] += 1
            return TriageResult(
                content=content, passed=False, score=utility,
                rejected_by=TriageGate.NOVELTY,
                reason="Duplicate content detected",
            )
        gates_passed.append("novelty")

        # Gate 3: Factual Accuracy
        factual = _factual_confidence(content)
        if factual < self._min_factual:
            self._stats["rejected"]["factual"] += 1
            return TriageResult(
                content=content, passed=False, score=utility,
                rejected_by=TriageGate.FACTUAL,
                factual_confidence=factual,
                reason=f"Low factual confidence: {factual:.2f}",
            )
        gates_passed.append("factual")

        # Gate 4: Safety
        if not _safety_check(content):
            self._stats["rejected"]["safety"] += 1
            return TriageResult(
                content=content, passed=False, score=utility,
                rejected_by=TriageGate.SAFETY,
                factual_confidence=factual,
                reason="PII or secrets detected",
            )
        gates_passed.append("safety")

        # All gates passed — register hash for future novelty checks
        self._known_hashes.add(h)
        self._stats["passed"] += 1

        return TriageResult(
            content=content, passed=True, score=utility,
            factual_confidence=factual, gates_passed=gates_passed,
        )

    def batch_evaluate(self, contents: list[str]) -> list[TriageResult]:
        return [self.evaluate(c) for c in contents]

    def status(self) -> dict:
        return {
            "known_hashes": len(self._known_hashes),
            **self._stats,
        }
