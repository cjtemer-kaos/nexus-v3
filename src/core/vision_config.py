"""
Vision Configuration - Configuracion centralizada de vision
"""
import os

def _ensure_protocol(url: str) -> str:
    if url and not url.startswith("http://") and not url.startswith("https://"):
        return "http://" + url
    return url or "http://localhost:11434"

_OLLAMA_URL = _ensure_protocol(os.getenv("OLLAMA_HOST", os.getenv("OLLAMA_URL", "http://localhost:11434")))

VISION_PROVIDERS = {
    "local": {
        "model": "qwen2.5vl:7b",
        "url": _OLLAMA_URL,
        "type": "ollama",
        "fallback": "pil",
    },
    "vision-gemma4": {
        "model": "gemma4:12b",
        "url": _OLLAMA_URL,
        "type": "ollama",
        "fallback": "local",
    },
    "vision-qwen35": {
        "model": "qwen3.5:9b",
        "url": _OLLAMA_URL,
        "type": "ollama",
        "fallback": "local",
    },
    "opencode_zen": {
        "model": "claude-sonnet-4-6",
        "url": "https://opencode.ai/zen/v1",
        "type": "openai",
        "api_key": os.getenv("OPENCODE_API_KEY", ""),
        "fallback": "openrouter",
    },
    "openrouter": {
        "model": "qwen-vl-plus:free",
        "url": "https://openrouter.ai/api/v1",
        "type": "openai",
        "api_key": os.getenv("OPENROUTER_API_KEY", ""),
        "fallback": "pil",
    },
    "pil": {
        "model": "pil_basic",
        "type": "pil",
        "fallback": None,
    },
}

DEFAULT_VISION_PROVIDER = os.getenv("DEFAULT_VISION_PROVIDER", "vision-qwen35")

MAX_IMAGE_SIZE_MB = 20

SUPPORTED_FORMATS = ["png", "jpg", "jpeg", "webp", "gif", "bmp"]

def get_vision_config(provider: str = None) -> dict:
    """Obtiene config del provider"""
    if provider is None:
        provider = DEFAULT_VISION_PROVIDER
    return VISION_PROVIDERS.get(provider, VISION_PROVIDERS["local"])

def get_all_providers() -> dict:
    """Lista todos los providers disponibles"""
    return {k: v.get("model", "unknown") for k, v in VISION_PROVIDERS.items()}
