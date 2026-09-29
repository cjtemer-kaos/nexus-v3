"""
PromptCompressor — LLMLingua-inspired prompt compression for SuperNEXUS v2.

Standalone module (DO NOT modify context_compactor.py).

Compression strategies:
  1. Remove filler words (English + Spanish)
  2. Collapse whitespace and blank lines
  3. Summarize repeated patterns (3+ identical sentence structures)
  4. Truncate verbose examples (keep first, note remaining)
  5. Preserve code blocks, URLs, file paths, @mentions intact
  6. Preserve important markers (CRITICAL, TODO, FIXME, WARNING)

Token estimation: bytes / 4 heuristic (grok-build pattern).
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("nexus-prompt-compressor")


# ─── Filler words (English + Spanish) ───────────────────────────────────

FILLER_WORDS_EN = {
    "basically", "actually", "really", "literally", "just",
    "like", "you know", "sort of", "kind of", "I mean",
    "well", "so", "right", "okay", "ok", "sure",
    "obviously", "clearly", "definitely", "absolutely",
    "honestly", "frankly", "seriously", "totally",
    "simply", "merely", "perhaps", "maybe",
}

FILLER_WORDS_ES = {
    "en realidad", "bueno", "pues", "o sea", "digamos",
    "como que", "más o menos", "tal vez", "a ver",
    "básicamente", "en realidad", "vamos a ver",
    "la verdad", "la cosa es", "te digo",
    "oye", "mira", "escucha",
}

# Combined filler words (sorted longest-first for matching)
FILLER_WORDS = sorted(
    FILLER_WORDS_EN | FILLER_WORDS_ES,
    key=lambda w: len(w),
    reverse=True,
)

# ─── Preservation markers ────────────────────────────────────────────────

IMPORTANT_MARKERS = re.compile(
    r"\b(CRITICAL|TODO|FIXME|WARNING|HACK|NOTE|IMPORTANT|SECURITY|BUG)\b",
    re.IGNORECASE,
)

# ─── Protected regions patterns ──────────────────────────────────────────

CODE_BLOCK_PATTERN = re.compile(r"(```[\s\S]*?```|`[^`\n]+`)", re.MULTILINE)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
FILE_PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/[\w.-]+(?:/[\w.-]+)*|\./[\w.-]+(?:/[\w.-]+)*)")
MENTION_PATTERN = re.compile(r"@\w+")

# ─── Example detection ──────────────────────────────────────────────────

EXAMPLE_MARKERS = re.compile(
    r"(?:^|\n)\s*(?:Example|Ejemplo|For example|Por ejemplo|e\.g\.|i\.e\.)\s*[:.]?\s*",
    re.IGNORECASE,
)


@dataclass
class CompressionMetrics:
    """Metrics from a compression run."""
    original_tokens: int = 0
    compressed_tokens: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 1.0
    filler_words_removed: int = 0
    blank_lines_collapsed: int = 0
    patterns_summarized: int = 0
    examples_truncated: int = 0
    protected_regions: int = 0


class PromptCompressor:
    """
    LLMLingua-inspired prompt compressor for SuperNEXUS v2.

    Compresses prompts by removing filler content while preserving
    semantically important elements (code, URLs, paths, markers).

    Usage:
        compressor = PromptCompressor()
        compressed = compressor.compress(long_prompt, target_ratio=0.5)
        tokens_saved = compressor.estimate_tokens(long_prompt) - compressor.estimate_tokens(compressed)
    """

    def __init__(
        self,
        filler_words: Optional[Dict[str, None]] = None,
        important_markers: Optional[re.Pattern] = None,
        min_pattern_count: int = 3,
    ):
        """
        Args:
            filler_words: Override default filler words set.
            important_markers: Override default important markers regex.
            min_pattern_count: Minimum occurrences to summarize a pattern (default: 3).
        """
        self.filler_words = filler_words if filler_words is not None else FILLER_WORDS
        self.important_markers = important_markers or IMPORTANT_MARKERS
        self.min_pattern_count = min_pattern_count

    # ─── Public API ──────────────────────────────────────────────────────

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count using bytes/4 heuristic.

        This is the grok-build token estimation pattern: each token is
        approximately 4 bytes of text for mixed English/Spanish content.

        Args:
            text: Input text to estimate.

        Returns:
            Estimated token count.
        """
        if not text:
            return 0
        return max(1, len(text.encode("utf-8")) // 4)

    def compress(
        self,
        text: str,
        target_ratio: float = 0.5,
        preserve_code: bool = True,
    ) -> str:
        """
        Compress a prompt using multiple strategies.

        Args:
            text: Input text to compress.
            target_ratio: Target compression ratio (0.5 = compress to ~50%).
            preserve_code: If True, protect code blocks, URLs, paths, @mentions.

        Returns:
            Compressed text with compression markers appended.
        """
        if not text or not text.strip():
            return text

        original_tokens = self.estimate_tokens(text)
        metrics = CompressionMetrics(original_tokens=original_tokens)

        # Step 0: Extract protected regions (code, URLs, paths, mentions)
        protected = {}
        working_text = text
        if preserve_code:
            working_text, protected = self._extract_protected(text)
            metrics.protected_regions = len(protected)

        # Step 1: Remove filler words
        working_text, filler_count = self._remove_filler_words(working_text)
        metrics.filler_words_removed = filler_count

        # Step 2: Collapse whitespace and blank lines
        working_text, blank_count = self._collapse_whitespace(working_text)
        metrics.blank_lines_collapsed = blank_count

        # Step 3: Summarize repeated patterns
        working_text, pattern_count = self._summarize_repeated_patterns(working_text)
        metrics.patterns_summarized = pattern_count

        # Step 4: Truncate verbose examples
        working_text, example_count = self._truncate_verbose_examples(working_text)
        metrics.examples_truncated = example_count

        # Step 5: Restore protected regions
        if preserve_code:
            working_text = self._restore_protected(working_text, protected)

        # Step 6: Check if we hit the target ratio; if not, do aggressive pass
        current_ratio = self.estimate_tokens(working_text) / max(original_tokens, 1)
        if current_ratio > target_ratio and target_ratio < 1.0:
            working_text = self._aggressive_compress(working_text, target_ratio, preserve_code)
            if preserve_code:
                # Re-extract and re-restore for aggressive pass
                working_text2, protected2 = self._extract_protected(working_text)
                working_text2 = self._aggressive_pass(working_text2)
                working_text = self._restore_protected(working_text2, protected2)

        # Build final result with compression marker
        compressed_tokens = self.estimate_tokens(working_text)
        ratio_pct = round((1 - compressed_tokens / max(original_tokens, 1)) * 100, 1)

        metrics.compressed_tokens = compressed_tokens
        metrics.tokens_saved = original_tokens - compressed_tokens
        metrics.compression_ratio = compressed_tokens / max(original_tokens, 1)

        marker = (
            f"<!--compressed from {original_tokens} to {compressed_tokens} tokens "
            f"(ratio: {ratio_pct}%)-->"
        )

        logger.debug(
            f"PromptCompressor: {original_tokens}→{compressed_tokens} tokens "
            f"({ratio_pct}% saved), fillers={metrics.filler_words_removed}, "
            f"patterns={metrics.patterns_summarized}, examples={metrics.examples_truncated}"
        )

        return f"{working_text}\n\n{marker}"

    def decompress_markers(self, text: str) -> str:
        """
        Remove compression markers from text.

        Strips the <!--compressed from N to M tokens (ratio: X%)--> markers
        and any related annotations, returning the clean compressed text.

        Args:
            text: Compressed text with markers.

        Returns:
            Text with compression markers removed.
        """
        # Remove compression markers (single or multiple)
        cleaned = re.sub(
            r"<!--compressed from \d+ to \d+ tokens \(ratio: [\d.]+%\)-->",
            "",
            text,
        )
        # Remove any trailing whitespace from marker removal
        cleaned = cleaned.rstrip()
        return cleaned

    # ─── Strategy 1: Filler word removal ─────────────────────────────────

    def _remove_filler_words(self, text: str) -> Tuple[str, int]:
        """Remove filler words while preserving structure."""
        count = 0
        result = text

        for filler in self.filler_words:
            # Word-boundary match for single words, flexible for phrases
            if " " in filler:
                # Multi-word filler: match with flexible whitespace
                pattern = re.compile(
                    r"\b" + re.escape(filler).replace(r"\ ", r"\s+") + r"\b",
                    re.IGNORECASE,
                )
            else:
                # Single word: strict word boundary
                pattern = re.compile(r"\b" + re.escape(filler) + r"\b", re.IGNORECASE)

            matches = pattern.findall(result)
            count += len(matches)
            result = pattern.sub("", result)

        # Clean up double spaces left by removal
        result = re.sub(r"  +", " ", result)
        # Clean up spaces before punctuation
        result = re.sub(r" ([.,;:!?])", r"\1", result)

        return result, count

    # ─── Strategy 2: Whitespace collapse ─────────────────────────────────

    def _collapse_whitespace(self, text: str) -> Tuple[str, int]:
        """Collapse multiple blank lines into single, strip trailing whitespace."""
        original_lines = text.split("\n")
        result_lines = []
        blank_count = 0
        prev_blank = False

        for line in original_lines:
            stripped = line.strip()
            if not stripped:
                if not prev_blank:
                    result_lines.append("")
                else:
                    blank_count += 1
                prev_blank = True
            else:
                result_lines.append(stripped)
                prev_blank = False

        result = "\n".join(result_lines).strip()
        return result, blank_count

    # ─── Strategy 3: Repeated pattern summarization ──────────────────────

    def _summarize_repeated_patterns(self, text: str) -> Tuple[str, int]:
        """
        If the same sentence structure appears 3+ times, replace with
        'N instances of: [pattern]'.
        """
        # Split into sentences
        sentences = re.split(r"(?<=[.!?])\s+", text)
        if len(sentences) < self.min_pattern_count:
            return text, 0

        # Normalize sentences to detect structure (strip specific values)
        def normalize(s: str) -> str:
            """Replace numbers, quoted strings, and paths with placeholders."""
            s = re.sub(r'"[^"]*"', '"<VAL>"', s)
            s = re.sub(r"'[^']*'", "'<VAL>'", s)
            s = re.sub(r"\b\d+\b", "<N>", s)
            s = re.sub(r"/[\w./-]+", "<PATH>", s)
            s = re.sub(r"@\w+", "<MENTION>", s)
            return s.lower().strip()

        # Group sentences by normalized structure
        # IMPORTANT: skip sentences with critical markers — never summarize them
        def has_important_marker(s: str) -> bool:
            return bool(self.important_markers.search(s))

        from collections import Counter
        structure_counter = Counter(
            normalize(s) for s in sentences if not has_important_marker(s)
        )
        repeated = {
            struct: count
            for struct, count in structure_counter.items()
            if count >= self.min_pattern_count
        }

        if not repeated:
            return text, 0

        # For each repeated pattern, keep first instance, summarize rest
        replaced_count = 0
        result_lines = []
        i = 0
        seen_patterns: Dict[str, int] = {}  # track which pattern we've seen

        while i < len(sentences):
            sent = sentences[i]

            # Always keep sentences with important markers
            if has_important_marker(sent):
                result_lines.append(sent)
                i += 1
                continue

            norm = normalize(sent)

            if norm in repeated:
                if norm not in seen_patterns:
                    # First occurrence — keep it
                    seen_patterns[norm] = 1
                    result_lines.append(sent)
                elif seen_patterns[norm] == 1:
                    # Second occurrence — start summarizing
                    seen_patterns[norm] = 2
                    count = repeated[norm]
                    result_lines.append(
                        f"[{count} instances of similar sentence omitted, first shown above]"
                    )
                    replaced_count += count - 1  # all but first
                # else: already summarized, skip
            else:
                result_lines.append(sent)

            i += 1

        result = " ".join(result_lines)
        return result, replaced_count

    # ─── Strategy 4: Example truncation ──────────────────────────────────

    def _truncate_verbose_examples(self, text: str) -> Tuple[str, int]:
        """
        Keep first example, note 'N more examples omitted'.
        Handles both single examples and example blocks.
        """
        # Find example blocks (Example 1, Example 2, etc.)
        example_pattern = re.compile(
            r"((?:Example|Ejemplo|For example|Por ejemplo)\s*\d*\s*[:.]\s*)",
            re.IGNORECASE,
        )
        matches = list(example_pattern.finditer(text))
        if len(matches) <= 1:
            return text, 0

        # Keep first example, summarize the rest
        first_match = matches[0]
        # Find where the first example ends (at next example or end of text)
        first_end = matches[1].start() if len(matches) > 1 else len(text)
        first_example = text[first_match.start():first_end].strip()

        # Summarize remaining examples
        remaining_count = len(matches) - 1
        summary = f"\n[{remaining_count} more examples omitted — see first example above]"

        # Rebuild text: everything before first example + first example + summary
        before = text[:first_match.start()].rstrip()
        after_end = len(text)

        result = f"{before}\n\n{first_example}\n{summary}"
        return result, remaining_count

    # ─── Protected region handling ────────────────────────────────────────

    def _extract_protected(self, text: str) -> Tuple[str, Dict[str, str]]:
        """
        Extract code blocks, URLs, file paths, and @mentions into placeholders.

        Returns:
            Tuple of (text with placeholders, dict of placeholder→original).
        """
        protected = {}
        counter = 0

        def make_placeholder(match: re.Match) -> str:
            nonlocal counter
            counter += 1
            key = f"__PROTECTED_{counter}__"
            protected[key] = match.group(0)
            return key

        result = text

        # Order matters: code blocks first (they may contain URLs/paths), then others
        result = CODE_BLOCK_PATTERN.sub(make_placeholder, result)
        result = URL_PATTERN.sub(make_placeholder, result)
        result = FILE_PATH_PATTERN.sub(make_placeholder, result)
        result = MENTION_PATTERN.sub(make_placeholder, result)

        return result, protected

    def _restore_protected(self, text: str, protected: Dict[str, str]) -> str:
        """Restore protected regions from placeholders."""
        result = text
        for key, original in protected.items():
            result = result.replace(key, original)
        return result

    # ─── Aggressive compression pass ─────────────────────────────────────

    def _aggressive_compress(
        self, text: str, target_ratio: float, preserve_code: bool
    ) -> str:
        """
        Second-pass aggressive compression when target_ratio not met.
        Removes short sentences, condenses lists, removes parenthetical asides.
        """
        # Remove parenthetical asides: ( ... )
        result = re.sub(r"\([^)]{10,}\)", "", text)

        # Remove sentences shorter than 10 chars (likely fragments)
        # BUT always keep sentences with important markers (CRITICAL, TODO, etc.)
        sentences = re.split(r"(?<=[.!?])\s+", result)
        result = " ".join(
            s for s in sentences
            if len(s.strip()) > 10 or self.important_markers.search(s)
        )

        # Collapse consecutive short bullet/list items
        result = re.sub(r"(?:^|\n)\s*[-*]\s*\S.{0,20}\n\s*[-*]\s*\S.{0,20}", "", result)

        # Final cleanup
        result = re.sub(r"  +", " ", result)
        result = re.sub(r"\n{3,}", "\n\n", result)

        return result.strip()

    def _aggressive_pass(self, text: str) -> str:
        """Ultra-aggressive pass: strip adverbs, condense clauses."""
        # Remove common adverbs
        adverbs = re.compile(
            r"\b(very|quite|rather|somewhat|fairly|extremely|highly|totally|"
            r"completely|entirely|absolutely|definitely|certainly|probably|"
            r"likely|essentially|basically|practically|virtually|mostly|"
            r"generally|typically|usually|normally|commonly|frequently|"
            r"regularly|often|rarely|seldom|occasionally|sometimes)\b",
            re.IGNORECASE,
        )
        result = adverbs.sub("", text)

        # Remove "that" connector where safe
        result = re.sub(r"\bthat\s+(?:is|are|was|were|has|have|had)\b", "", result, flags=re.IGNORECASE)

        # Final cleanup
        result = re.sub(r"  +", " ", result)
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip()


# ─── Convenience functions ────────────────────────────────────────────────

_default_compressor: Optional[PromptCompressor] = None


def get_compressor() -> PromptCompressor:
    """Get or create the default PromptCompressor singleton."""
    global _default_compressor
    if _default_compressor is None:
        _default_compressor = PromptCompressor()
    return _default_compressor


def compress_prompt(
    text: str,
    target_ratio: float = 0.5,
    preserve_code: bool = True,
) -> str:
    """
    Convenience function to compress a prompt.

    Args:
        text: Input text.
        target_ratio: Target compression ratio (0.5 = 50% of original).
        preserve_code: Protect code blocks, URLs, paths, @mentions.

    Returns:
        Compressed text with compression markers.
    """
    return get_compressor().compress(text, target_ratio, preserve_code)


def estimate_prompt_tokens(text: str) -> int:
    """
    Convenience function to estimate token count.

    Args:
        text: Input text.

    Returns:
        Estimated token count (bytes / 4 heuristic).
    """
    return get_compressor().estimate_tokens(text)


def decompress_prompt_markers(text: str) -> str:
    """
    Convenience function to remove compression markers.

    Args:
        text: Compressed text with markers.

    Returns:
        Text with markers removed.
    """
    return get_compressor().decompress_markers(text)
