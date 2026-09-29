"""
System Prompts Advanced - SuperNEXUS v2
Temperature control, prefix/suffix injection, preset system.
"""

import json
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "prompts"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PRESETS_FILE = DATA_DIR / "presets.json"

DEFAULT_PRESETS = {
    "default": {
        "name": "Default",
        "temperature": 0.7,
        "max_tokens": 2048,
        "system_prompt": "",
        "inject_prefix": "",
        "inject_suffix": "",
        "enabled": True,
    },
    "code_analyze": {
        "name": "Code Analysis",
        "temperature": 0.2,
        "max_tokens": 8000,
        "system_prompt": "You are a senior code analyzer. Be precise, find bugs, suggest improvements.",
        "inject_prefix": "",
        "inject_suffix": "",
        "enabled": True,
    },
    "brainstorm": {
        "name": "Brainstorm",
        "temperature": 0.9,
        "max_tokens": 4096,
        "system_prompt": "You are a creative ideation assistant. Generate many diverse ideas.",
        "inject_prefix": "",
        "inject_suffix": "",
        "enabled": True,
    },
    "reason": {
        "name": "Reasoning",
        "temperature": 0.3,
        "max_tokens": 6000,
        "system_prompt": "You are a systematic reasoning assistant. Think step by step.",
        "inject_prefix": "",
        "inject_suffix": "",
        "enabled": True,
    },
    "concise": {
        "name": "Concise",
        "temperature": 0.5,
        "max_tokens": 512,
        "system_prompt": "Respond in 2-3 sentences max. Be extremely concise.",
        "inject_prefix": "",
        "inject_suffix": "",
        "enabled": True,
    },
    "creative": {
        "name": "Creative Writing",
        "temperature": 1.0,
        "max_tokens": 4096,
        "system_prompt": "You are a creative writer. Use vivid language, metaphors, and engaging narrative.",
        "inject_prefix": "",
        "inject_suffix": "",
        "enabled": True,
    },
}

REASONING_MODELS = {"o1", "o3", "o4", "gpt-5", "deepseek-r1"}


class PromptManager:
    """Gestor de presets de prompts con temperature y prefix/suffix."""

    def __init__(self):
        self.presets: Dict[str, Dict] = {}
        self.active_preset: str = "default"
        self._load()

    def _load(self):
        try:
            if PRESETS_FILE.exists():
                data = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))
                self.presets = data.get("presets", {})
                self.active_preset = data.get("active", "default")
            else:
                self.presets = dict(DEFAULT_PRESETS)
        except Exception as e:
            logger.error(f"Error cargando presets: {e}")
            self.presets = dict(DEFAULT_PRESETS)

    def _save(self):
        try:
            PRESETS_FILE.write_text(json.dumps({
                "presets": self.presets,
                "active": self.active_preset,
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error guardando presets: {e}")

    def get_active(self) -> Dict:
        return self.presets.get(self.active_preset, DEFAULT_PRESETS["default"])

    def set_active(self, name: str) -> bool:
        if name in self.presets:
            self.active_preset = name
            self._save()
            return True
        return False

    def create_preset(self, name: str, data: Dict) -> Dict:
        preset = {
            "name": name,
            "temperature": data.get("temperature", 0.7),
            "max_tokens": data.get("max_tokens", 2048),
            "system_prompt": data.get("system_prompt", ""),
            "inject_prefix": data.get("inject_prefix", ""),
            "inject_suffix": data.get("inject_suffix", ""),
            "enabled": data.get("enabled", True),
        }
        self.presets[name] = preset
        self._save()
        return preset

    def update_preset(self, name: str, updates: Dict) -> Optional[Dict]:
        if name not in self.presets:
            return None
        self.presets[name].update(updates)
        self._save()
        return self.presets[name]

    def delete_preset(self, name: str) -> bool:
        if name in self.presets and name != "default":
            del self.presets[name]
            self._save()
            return True
        return False

    def list_presets(self) -> Dict[str, Dict]:
        return dict(self.presets)

    def get_temperature(self, model: str = "") -> float:
        """Obtener temperatura para el modelo actual"""
        preset = self.get_active()
        temp = preset.get("temperature", 0.7)
        for prefix in REASONING_MODELS:
            if model.startswith(prefix):
                return 1.0
        return max(0.0, min(1.0, temp))

    def apply_prompt(self, messages: List[Dict], model: str = "") -> List[Dict]:
        """Aplicar prefix/suffix/system_prompt a mensajes"""
        preset = self.get_active()
        prefix = preset.get("inject_prefix", "")
        suffix = preset.get("inject_suffix", "")
        system_prompt = preset.get("system_prompt", "")

        result = list(messages)

        if system_prompt:
            if result and result[0].get("role") == "system":
                result[0]["content"] = system_prompt + "\n\n" + result[0]["content"]
            else:
                result.insert(0, {"role": "system", "content": system_prompt})

        if prefix:
            for msg in result:
                if msg.get("role") == "user":
                    msg["content"] = prefix + msg["content"]
                    break

        if suffix:
            for msg in reversed(result):
                if msg.get("role") == "user":
                    msg["content"] = msg["content"] + suffix
                    break

        return result

    def get_status(self) -> Dict:
        return {
            "active_preset": self.active_preset,
            "total_presets": len(self.presets),
            "temperature": self.get_temperature(),
        }
