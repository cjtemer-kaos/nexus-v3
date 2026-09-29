"""SkillAwareDirector — chat with skills injected into LLM context."""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("skill-aware-director")


class SkillAwareDirector:
    """Director that injects relevant skills into LLM chat context."""

    def __init__(self, director, ollama_client):
        self._director = director
        self._ollama = ollama_client
        self._stats = {"chats": 0, "skills_injected": 0}

    async def chat(self, message: str, context: str = "", model: str = "") -> Dict[str, Any]:
        """Chat with skill-aware context injection."""
        self._stats["chats"] += 1
        if not model:
            model = self._ollama._default_model if self._ollama else "gemma2:9b"
        result = await self._director.dispatch("ayuda", message, context=context)
        return {
            "response": result.get("response", str(result)),
            "model": model,
            "skills_used": [],
        }

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)


_instance: Optional[SkillAwareDirector] = None


def get_skill_aware_director(director=None, ollama_client=None) -> SkillAwareDirector:
    global _instance
    if _instance is None and director is not None:
        _instance = SkillAwareDirector(director, ollama_client)
    return _instance or SkillAwareDirector(director, ollama_client)
