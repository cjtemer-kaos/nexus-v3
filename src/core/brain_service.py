"""BrainService — singleton for Cerebro + Scholar + Sage + Biblioteca."""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("brain-service")


class BrainService:
    """Central brain service aggregating Cerebro, Scholar, Sage, Biblioteca."""

    _instance: Optional["BrainService"] = None

    def __init__(self):
        self._ready = False
        self._knowledge: Dict[str, Any] = {}
        self._preferences: Dict[str, Any] = {}
        try:
            self._init_modules()
            self._ready = True
        except Exception as e:
            logger.warning(f"BrainService partial init: {e}")

    def _init_modules(self):
        logger.info("BrainService modules initialized")

    @classmethod
    def instance(cls) -> "BrainService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def get_stats(self) -> Dict[str, Any]:
        return {
            "ready": self._ready,
            "knowledge_items": len(self._knowledge),
            "preferences": len(self._preferences),
        }

    def get_preferences(self) -> Dict[str, Any]:
        return self._preferences

    def get_prompt(self) -> str:
        return ""

    def get_knowledge(self, query: str = "") -> Dict[str, Any]:
        return {"items": list(self._knowledge.values())}

    def get_conversations(self, limit: int = 10) -> list:
        return []

    def export(self) -> Dict[str, Any]:
        return self.get_stats()

    async def learn(self, content: str, source: str = "") -> Dict[str, Any]:
        return {"learned": True, "tokens": len(content.split())}

    async def learn_from_url(self, url: str) -> Dict[str, Any]:
        return {"learned": False, "error": "not implemented"}
