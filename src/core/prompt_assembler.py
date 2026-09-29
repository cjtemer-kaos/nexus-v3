"""
Prompt Assembler - Three-tier prompt assembly with caching.
Absorbed from hermes-agent — names cleaned.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class PromptAssembler:
    """Three-tier prompt assembly: stable + context + volatile."""

    def __init__(self, data_dir: Optional[Path] = None):
        self.data_dir = data_dir or Path(os.environ.get("APP_DATA", Path.home() / ".app")) / "prompts"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._cache: Dict[str, str] = {}
        self._cache_hash: Optional[str] = None
        self._snapshot_file = self.data_dir / ".prompt_snapshot.json"

    def build_system_prompt(
        self,
        identity: str = "",
        tool_guidance: str = "",
        skills: str = "",
        environment: str = "",
        context_files: str = "",
        system_message: str = "",
        memory_snapshot: str = "",
        user_context: str = "",
        timestamp: str = "",
        session_info: str = "",
    ) -> str:
        """Build system prompt in three tiers for cache friendliness."""
        stable_parts = []
        if identity:
            stable_parts.append(identity)
        if tool_guidance:
            stable_parts.append(tool_guidance)
        if skills:
            stable_parts.append(skills)
        if environment:
            stable_parts.append(environment)

        context_parts = []
        if context_files:
            context_parts.append(context_files)
        if system_message:
            context_parts.append(system_message)

        volatile_parts = []
        if memory_snapshot:
            volatile_parts.append(memory_snapshot)
        if user_context:
            volatile_parts.append(user_context)
        if timestamp:
            volatile_parts.append(f"Current time: {timestamp}")
        if session_info:
            volatile_parts.append(session_info)

        stable = "\n\n".join(stable_parts)
        context = "\n\n".join(context_parts)
        volatile = "\n\n".join(volatile_parts)

        full = "\n\n".join(p for p in (stable, context, volatile) if p)

        self._cache_hash = hashlib.sha256(full.encode()).hexdigest()[:16]
        self._cache["full"] = full
        self._cache["stable"] = stable
        self._cache["context"] = context
        self._cache["volatile"] = volatile

        return full

    def get_cached(self) -> Optional[str]:
        return self._cache.get("full")

    def invalidate(self):
        self._cache.clear()
        self._cache_hash = None

    def save_snapshot(self):
        snapshot = {
            "hash": self._cache_hash,
            "timestamp": time.time(),
            "stable_length": len(self._cache.get("stable", "")),
            "context_length": len(self._cache.get("context", "")),
        }
        self._snapshot_file.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    def load_snapshot(self) -> Optional[Dict]:
        if self._snapshot_file.exists():
            try:
                return json.loads(self._snapshot_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return None

    def validate_prompt(self, prompt: str, max_length: int = 100000) -> tuple:
        """Validate assembled prompt. Returns (ok, reason)."""
        if not prompt:
            return False, "Empty prompt"
        if len(prompt) > max_length:
            return False, f"Prompt too long ({len(prompt)} > {max_length})"
        return True, "ok"

    def get_stats(self) -> Dict:
        return {
            "cached": bool(self._cache),
            "hash": self._cache_hash,
            "stable_size": len(self._cache.get("stable", "")),
            "context_size": len(self._cache.get("context", "")),
            "volatile_size": len(self._cache.get("volatile", "")),
        }

    # ---- FASE 2.3: Prompt Optimization Pipeline ----
    # Inspired by DSPy + prompto-lab: automatic prompt compression, scoring,
    # and iterative improvement for the Director v6 system prompt.

    FILLER_WORDS = {
        "please", "kindly", "just", "simply", "basically", "actually", "really",
        "very", "quite", "somewhat", "rather", "pretty much", "in order to",
        "for the purpose of", "it is important to note that", "it should be noted",
        "as a matter of fact", "in terms of", "with regard to", "in the event that",
        "due to the fact that", "in light of the fact that", "it goes without saying",
        "needless to say", "at this point in time", "in the near future",
    }

    def optimize_prompt(self, prompt: str) -> Dict:
        """Optimize a prompt by compressing filler, scoring quality, and suggesting improvements.

        Returns:
            {
                "original_length": int,
                "optimized": str,
                "optimized_length": int,
                "savings_percent": float,
                "quality_score": float (0-100),
                "issues": list[str],
                "suggestions": list[str],
            }
        """
        issues = []
        suggestions = []
        optimized = prompt

        # 1. Remove filler words
        original_len = len(prompt)
        for filler in sorted(self.FILLER_WORDS, key=len, reverse=True):
            optimized = optimized.replace(filler, "")

        # 2. Collapse multiple whitespace
        import re
        optimized = re.sub(r'\n{3,}', '\n\n', optimized)
        optimized = re.sub(r' {2,}', ' ', optimized)
        optimized = optimized.strip()

        # 3. Score quality
        score = 100.0
        lines = prompt.split('\n')
        non_empty = [l for l in lines if l.strip()]

        # Length penalties
        if len(prompt) > 50000:
            score -= 15
            issues.append(f"Prompt very long ({len(prompt)} chars)")
            suggestions.append("Consider chunking into stable/context/volatile tiers")
        elif len(prompt) > 20000:
            score -= 5
            issues.append(f"Prompt is long ({len(prompt)} chars)")

        # Structure penalties
        if not any('##' in l or '#' in l for l in non_empty[:20]):
            score -= 5
            suggestions.append("Add section headers (##) for better LLM navigation")

        # Filler detection
        filler_count = sum(1 for f in self.FILLER_WORDS if f in prompt.lower())
        if filler_count > 3:
            score -= min(20, filler_count * 2)
            issues.append(f"{filler_count} filler words detected")
            suggestions.append("Remove filler words for concise instructions")

        # Repetition detection
        sentences = [s.strip() for s in re.split(r'[.!?]+', prompt) if len(s.strip()) > 20]
        if len(sentences) > 5:
            unique_ratio = len(set(sentences)) / len(sentences)
            if unique_ratio < 0.8:
                score -= 10
                issues.append(f"Repetitive content detected (unique ratio: {unique_ratio:.0%})")
                suggestions.append("Deduplicate repetitive instructions")

        # Positive signals
        if 'example' in prompt.lower() or 'ejemplo' in prompt.lower():
            score += 5
        if any(marker in prompt for marker in ['---', '##', '###', '- ', '1.']):
            score += 5
            suggestions.append("Good use of structured formatting")

        score = max(0, min(100, score))
        savings = ((original_len - len(optimized)) / original_len * 100) if original_len > 0 else 0

        return {
            "original_length": original_len,
            "optimized": optimized,
            "optimized_length": len(optimized),
            "savings_percent": round(savings, 1),
            "quality_score": round(score, 1),
            "issues": issues,
            "suggestions": suggestions,
        }

    def compress_for_tokens(self, prompt: str, target_tokens: int = 4000) -> str:
        """Compress a prompt to fit within a token budget (approx 4 chars/token).

        Priority: identity > tools > skills > context > volatile.
        Trims from the bottom (volatile) first.
        """
        chars_budget = target_tokens * 4
        if len(prompt) <= chars_budget:
            return prompt

        # Split by tiers if cached
        stable = self._cache.get("stable", "")
        context = self._cache.get("context", "")
        volatile = self._cache.get("volatile", "")

        if stable and context and volatile:
            # Already tiered — trim volatile first, then context
            if len(stable) + len(context) + len(volatile) <= chars_budget:
                # Need to trim volatile
                available = chars_budget - len(stable) - len(context)
                if available > 0:
                    volatile = volatile[:available]
                    return f"{stable}\n\n{context}\n\n{volatile}"
                # Need to trim context too
                available = chars_budget - len(stable)
                context = context[:max(0, available)]
                return f"{stable}\n\n{context}"

        # Fallback: truncate from the end
        return prompt[:chars_budget] + "\n\n[... truncated to fit token budget]"
