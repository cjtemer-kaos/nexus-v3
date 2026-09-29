"""
Trajectory Compressor - Compress conversation history for token optimization.

Adapted for SuperNEXUS v2.0
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum

logger = logging.getLogger(__name__)


class TurnType(Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    TOOL_RESULT = "tool_result"


@dataclass
class Turn:
    turn_type: TurnType
    content: str
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CompressionResult:
    original_turns: int
    compressed_turns: int
    original_tokens: int
    compressed_tokens: int
    summary: str
    preserved_turns: List[Turn]
    compressed_region: List[Turn]


class TrajectoryCompressor:
    """Compress conversation trajectories while preserving training signal quality."""

    def __init__(self, target_max_tokens: int = 16000, preserve_first_n: int = 2, preserve_last_n: int = 3):
        self.target_max_tokens = target_max_tokens
        self.preserve_first_n = preserve_first_n
        self.preserve_last_n = preserve_last_n

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4

    def compress(self, turns: List[Turn], current_tokens: int = None) -> CompressionResult:
        if not turns:
            return CompressionResult(0, 0, 0, 0, "", [], [])
        
        original_count = len(turns)
        
        if current_tokens is None:
            current_tokens = sum(self.estimate_tokens(t.content) for t in turns)
        
        if current_tokens <= self.target_max_tokens:
            return CompressionResult(original_count, original_count, current_tokens, current_tokens, "", turns, [])
        
        first_region = turns[:self.preserve_first_n]
        last_region = turns[-self.preserve_last_n:] if self.preserve_last_n > 0 else []
        
        middle_start = self.preserve_first_n
        middle_end = len(turns) - self.preserve_last_n if self.preserve_last_n > 0 else len(turns)
        middle_region = turns[middle_start:middle_end] if middle_end > middle_start else []
        
        summary = self._generate_summary(middle_region)
        
        preserved = list(first_region)
        if summary:
            preserved.append(Turn(
                turn_type=TurnType.ASSISTANT,
                content=f"[Previous {len(middle_region)} turns summarized]: {summary}",
                metadata={"compressed": True, "original_count": len(middle_region)},
            ))
        preserved.extend(last_region)
        
        compressed_tokens = sum(self.estimate_tokens(t.content) for t in preserved)
        
        logger.info(f"[compressor] {original_count} turns -> {len(preserved)} turns, {current_tokens} -> {compressed_tokens} tokens")
        
        return CompressionResult(original_count, len(preserved), current_tokens, compressed_tokens, summary, preserved, middle_region)

    def _generate_summary(self, middle_region: List[Turn]) -> str:
        if not middle_region:
            return ""
        
        user_messages = [t.content for t in middle_region if t.turn_type == TurnType.USER]
        tool_calls = [t.tool_name for t in middle_region if t.turn_type == TurnType.TOOL]
        
        summary_parts = []
        
        if user_messages:
            summary_parts.append(f"User asked about: {user_messages[0][:100]}")
            if len(user_messages) > 1:
                summary_parts.append(f"... and {len(user_messages) - 1} more questions")
        
        if tool_calls:
            unique_tools = list(set(tool_calls))
            summary_parts.append(f"Tools used: {', '.join(unique_tools[:5])}")
        
        return " | ".join(summary_parts) if summary_parts else f"{len(middle_region)} turns"

    def should_compress(self, turns: List[Turn]) -> bool:
        if not turns:
            return False
        total_tokens = sum(self.estimate_tokens(t.content) for t in turns)
        return total_tokens > self.target_max_tokens

    def compress_if_needed(self, turns: List[Turn]) -> List[Turn]:
        if self.should_compress(turns):
            result = self.compress(turns)
            return result.preserved_turns
        return turns


_compressor: Optional[TrajectoryCompressor] = None


def get_trajectory_compressor(target_max_tokens: int = 16000, preserve_first_n: int = 2, preserve_last_n: int = 3) -> TrajectoryCompressor:
    global _compressor
    if _compressor is None:
        _compressor = TrajectoryCompressor(target_max_tokens=target_max_tokens, preserve_first_n=preserve_first_n, preserve_last_n=preserve_last_n)
        logger.info(f"[compressor] Initialized with target={target_max_tokens} tokens")
    return _compressor