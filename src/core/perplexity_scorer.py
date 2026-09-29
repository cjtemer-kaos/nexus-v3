# src/core/perplexity_scorer.py
"""
Perplexity Scorer — Information density scoring for compression.

Approximates perplexity WITHOUT an LLM using:
- Vocabulary diversity (unique words / total words)
- Character entropy (Shannon entropy of character distribution)
- Repetition penalty (n-gram repetition ratio)
- Code detection bonus (technical content is always high-value)

Score: 0.0 (trivial, compress first) to 1.0 (dense, preserve).

Used by ContextCompactor to prioritize which messages to compress.
"""
from __future__ import annotations

import math
import re
import logging
from collections import Counter

logger = logging.getLogger(__name__)


class PerplexityScorer:
    """Scores text by information density. No LLM needed."""

    def score(self, text: str) -> float:
        """Score 0.0 (low info) to 1.0 (high info)."""
        if not text or not text.strip():
            return 0.0

        words = re.findall(r'\w+', text.lower())
        if len(words) < 2:
            return 0.1

        # Component 1: Vocabulary diversity (0-1)
        unique_ratio = len(set(words)) / len(words)
        vocab_score = min(unique_ratio * 1.2, 1.0)

        # Component 2: Character entropy (0-1)
        char_freq = Counter(text.lower())
        total_chars = sum(char_freq.values())
        entropy = -sum(
            (c / total_chars) * math.log2(c / total_chars)
            for c in char_freq.values() if c > 0
        )
        # Normalize: English text typically has entropy 3.5-4.5 bits/char
        entropy_score = min(entropy / 5.0, 1.0)

        # Component 3: Repetition penalty (0-1, higher = less repetition = better)
        bigrams = [f"{words[i]}_{words[i+1]}" for i in range(len(words) - 1)]
        if bigrams:
            bigram_unique = len(set(bigrams)) / len(bigrams)
        else:
            bigram_unique = 1.0
        repetition_score = bigram_unique

        # Component 4: Code/technical bonus
        code_indicators = ["()", "->", "=>", "def ", "class ", "import ", "return ",
                           "{}", "[]", "==", "!=", "&&", "||", "async ", "await "]
        code_bonus = 0.15 if any(ind in text for ind in code_indicators) else 0.0

        # Weighted combination
        score = (
            vocab_score * 0.30 +
            entropy_score * 0.25 +
            repetition_score * 0.30 +
            code_bonus +
            0.0  # base
        )
        return max(0.0, min(1.0, score))

    def rank_by_density(self, messages: list[dict]) -> list[dict]:
        """Rank messages by information density (highest first)."""
        scored = []
        for msg in messages:
            content = msg.get("content", "")
            s = self.score(content)
            scored.append((s, msg))
        scored.sort(key=lambda x: -x[0])
        return [msg for _, msg in scored]

    def compression_candidates(self, messages: list[dict], threshold: float = 0.3) -> list[int]:
        """Return indices of messages below density threshold (compress these first)."""
        candidates = []
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if self.score(content) < threshold:
                candidates.append(i)
        return candidates
