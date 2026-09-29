"""
Background Review Daemon — F21

After every conversation turn, spawns a background task that reviews
the session and decides what to save to memory or update in skills.
Runs asynchronously without blocking the main thread.

Adapted for SuperNEXUS v2: uses Cerebro + KnowledgeVault + Director
"""

import asyncio
import logging
from typing import Dict

logger = logging.getLogger("nexus-review")

_MEMORY_REVIEW_PROMPT = (
    "Review the conversation and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their "
    "work style, or ways they want you to operate?\n\n"
    "If something stands out, save it. If nothing is worth saving, say 'Nothing to save.'"
)

_SKILL_REVIEW_PROMPT = (
    "Review the conversation and consider what should be saved as a skill or "
    "knowledge update. Be ACTIVE — most sessions produce at least one update.\n\n"
    "Signals to look for (any one warrants action):\n"
    "  • User corrected your style, tone, format, verbosity, or approach.\n"
    "  • User corrected your workflow, approach, or sequence of steps.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path emerged.\n"
    "  • A skill that was loaded turned out wrong, missing, or outdated.\n\n"
    "Do NOT capture:\n"
    "  • Environment-dependent failures (missing binaries, uninstalled packages).\n"
    "  • Negative claims about tools ('X tool is broken').\n"
    "  • Session-specific transient errors that resolved.\n"
    "  • One-off task narratives.\n\n"
    "'Nothing to save.' is a real option but should NOT be the default."
)


class BackgroundReviewDaemon:
    """Reviews conversations post-turn and auto-saves learnings"""

    def __init__(self, director=None, cerebro=None, vault=None):
        self.director = director
        self.cerebro = cerebro
        self.vault = vault
        self._enabled = True
        self._reviews_run = 0
        self._learnings_saved = 0

    def configure(self, enabled: bool = True):
        self._enabled = enabled

    async def spawn_review(self, conversation_history: list, session_id: str = ""):
        """Spawn background review task — non-blocking"""
        if not self._enabled:
            return
        if len(conversation_history) < 2:
            return  # Skip very short conversations

        task = asyncio.create_task(
            self._run_review(conversation_history, session_id),
            name=f"review-{session_id}",
        )
        task.add_done_callback(self._review_done)
        logger.debug(f"Background review spawned for session {session_id}")

    async def _run_review(self, history: list, session_id: str):
        """Execute the review logic"""
        try:
            # Build review context from conversation
            context = self._build_context(history)

            # Memory review
            memory_result = await self._review_memory(context, session_id)

            # Skill/knowledge review
            skill_result = await self._review_skills(context, session_id)

            self._reviews_run += 1
            saved = 0
            if memory_result.get("saved"):
                saved += 1
            if skill_result.get("saved"):
                saved += 1
            self._learnings_saved += saved

            if saved > 0:
                logger.info(f"Review complete for {session_id}: {saved} learning(s) saved")

        except Exception as e:
            logger.warning(f"Background review failed for {session_id}: {e}")

    def _build_context(self, history: list) -> str:
        """Build review context from conversation history"""
        lines = []
        for msg in history[-20:]:  # Last 20 messages
            role = msg.get("role", "unknown")
            content = msg.get("content", "")[:500]
            lines.append(f"{role}: {content}")
        return "\n".join(lines)

    async def _review_memory(self, context: str, session_id: str) -> Dict:
        """Review for memory-worthy information"""
        if not self.cerebro:
            return {"saved": False, "reason": "cerebro not available"}

        try:
            # Use a lightweight model for review
            if self.director and self.director.ai_tools:
                result = await self.director.ai_tools.quick_response(
                    task=f"{_MEMORY_REVIEW_PROMPT}\n\nConversation:\n{context}",
                    gem="sage",
                    context="",
                )
                content = result.get("content", "")
                if content and "nothing to save" not in content.lower():
                    # Save to cerebro
                    await self.cerebro.aprender_interaccion(
                        f"Review insight from session {session_id}",
                        content,
                        "sage",
                    )
                    return {"saved": True, "content": content[:200]}
        except Exception as e:
            logger.debug(f"Memory review error: {e}")
        return {"saved": False}

    async def _review_skills(self, context: str, session_id: str) -> Dict:
        """Review for skill/knowledge updates"""
        if not self.vault:
            return {"saved": False, "reason": "vault not available"}

        try:
            if self.director and self.director.ai_tools:
                result = await self.director.ai_tools.quick_response(
                    task=f"{_SKILL_REVIEW_PROMPT}\n\nConversation:\n{context}",
                    gem="scholar",
                    context="",
                )
                content = result.get("content", "")
                if content and "nothing to save" not in content.lower():
                    # Save to knowledge vault
                    self.vault.add_note(
                        title=f"Review {session_id}",
                        content=content,
                        category="review",
                        tags=["auto-learned", session_id],
                    )
                    return {"saved": True, "content": content[:200]}
        except Exception as e:
            logger.debug(f"Skill review error: {e}")
        return {"saved": False}

    def _review_done(self, task):
        """Callback when review completes"""
        try:
            task.result()
        except Exception as e:
            logger.debug(f"Background review task completed with note: {e}")

    def get_stats(self) -> Dict:
        return {
            "enabled": self._enabled,
            "reviews_run": self._reviews_run,
            "learnings_saved": self._learnings_saved,
        }
