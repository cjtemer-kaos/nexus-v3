"""
Personas - SuperNEXUS v2
Custom agents con system prompt, modelo, herramientas y memoria propias.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "personas"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PERSONAS_FILE = DATA_DIR / "personas.json"


class Persona:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", str(uuid.uuid4())[:8])
        self.name: str = data.get("name", "")
        self.avatar: str = data.get("avatar", "")
        self.personality: str = data.get("personality", "")
        self.model: str = data.get("model", "qwen2.5-coder:7b")
        self.endpoint_url: str = data.get("endpoint_url", "")
        self.greeting: str = data.get("greeting", "")
        self.enabled_tools: List[str] = data.get("enabled_tools", ["all"])
        self.user_name: str = data.get("user_name", "")
        self.timezone: str = data.get("timezone", "America/Mexico_City")
        self.temperature: float = data.get("temperature", 0.7)
        self.max_tokens: int = data.get("max_tokens", 2048)
        self.is_active: bool = data.get("is_active", True)
        self.is_default: bool = data.get("is_default", False)
        self.created_at: float = data.get("created_at", time.time())
        self.session_id: Optional[str] = data.get("session_id")
        self.metadata: Dict = data.get("metadata", {})

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "name": self.name, "avatar": self.avatar,
            "personality": self.personality, "model": self.model,
            "endpoint_url": self.endpoint_url, "greeting": self.greeting,
            "enabled_tools": self.enabled_tools, "user_name": self.user_name,
            "timezone": self.timezone, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "is_active": self.is_active,
            "is_default": self.is_default, "created_at": self.created_at,
            "session_id": self.session_id, "metadata": self.metadata,
        }

    def get_system_prompt(self, current_time: str = "") -> str:
        """Construir system prompt completo"""
        parts = []
        if current_time:
            parts.append(f"Hora actual: {current_time}")
        if self.personality:
            parts.append(self.personality)
        if self.user_name:
            parts.append(f"El usuario se llama {self.user_name}.")
        return "\n\n".join(parts) if parts else f"Eres {self.name}, un asistente de SuperNEXUS."


class PersonaManager:
    """Gestor de personas custom."""

    def __init__(self):
        self.personas: Dict[str, Persona] = {}
        self._load()

    def _load(self):
        try:
            if PERSONAS_FILE.exists():
                data = json.loads(PERSONAS_FILE.read_text(encoding="utf-8"))
                for p in data.get("personas", []):
                    persona = Persona(p)
                    self.personas[persona.id] = persona
        except Exception as e:
            logger.error(f"Error cargando personas: {e}")

    def _save(self):
        try:
            PERSONAS_FILE.write_text(json.dumps({
                "personas": [p.to_dict() for p in self.personas.values()]
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando personas: {e}")

    def create(self, data: Dict) -> Persona:
        persona = Persona({"created_at": time.time(), **data})
        self.personas[persona.id] = persona
        self._save()
        logger.info(f"Persona creada: {persona.name} ({persona.id})")
        return persona

    def update(self, persona_id: str, updates: Dict) -> Optional[Persona]:
        persona = self.personas.get(persona_id)
        if not persona:
            return None
        for k, v in updates.items():
            if hasattr(persona, k):
                setattr(persona, k, v)
        self._save()
        return persona

    def delete(self, persona_id: str) -> bool:
        if persona_id in self.personas:
            del self.personas[persona_id]
            self._save()
            return True
        return False

    def get(self, persona_id: str) -> Optional[Persona]:
        return self.personas.get(persona_id)

    def get_default(self) -> Optional[Persona]:
        for p in self.personas.values():
            if p.is_default:
                return p
        return None

    def list_personas(self, active_only: bool = False) -> List[Dict]:
        personas = list(self.personas.values())
        if active_only:
            personas = [p for p in personas if p.is_active]
        return [p.to_dict() for p in personas]

    def get_by_name(self, name: str) -> Optional[Persona]:
        name_lower = name.lower()
        for p in self.personas.values():
            if p.name.lower() == name_lower:
                return p
        return None

    def get_status(self) -> Dict:
        return {
            "total": len(self.personas),
            "active": sum(1 for p in self.personas.values() if p.is_active),
            "default": self.get_default().name if self.get_default() else None,
        }
