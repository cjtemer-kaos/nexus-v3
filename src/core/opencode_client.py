"""
OpenCode Zen Integration - LLM cloud para SuperNEXUS v2.0

Conexión a OpenCode Zen API para acceso a modelos premium (Claude, GPT, Gemini, Qwen)
sin necesidad de GPU local ni Ollama.

Endpoints:
- OpenAI-compatible: https://opencode.ai/zen/v1/chat/completions (Qwen, MiniMax, GLM, Kimi, DeepSeek, Nemotron)
- Anthropic: https://opencode.ai/zen/v1/messages (Claude)
- OpenAI: https://opencode.ai/zen/v1/responses (GPT)

Modelos gratuitos: DeepSeek V4 Flash Free, MiniMax M2.5 Free, Nemotron 3 Super Free, Big Pickle

Documentación: https://opencode.ai/docs/es/zen/
"""

import logging
import os
from typing import Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv(encoding="utf-8-sig")

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Base URL
ZEN_BASE = "https://opencode.ai/zen/v1"

# Modelos recomendados por categoría (IDs de Zen)
ZEN_MODELS = {
    "chat": "qwen3.6-plus",
    "code": "qwen3.6-plus",
    "reasoning": "qwen3.6-plus",
    "vision": "qwen3.6-plus",
    "fast": "qwen3.5-plus",
    "premium": "claude-sonnet-4-6",
    "free": "deepseek-v4-flash-free",
}

# Modelos gratuitos
FREE_MODELS = [
    "deepseek-v4-flash-free",
    "minimax-m2.5-free",
    "nemotron-3-super-free",
    "big-pickle",
]

# Mapeo de modelo a endpoint
MODEL_ENDPOINTS = {
    # OpenAI-compatible (chat/completions)
    "qwen3.6-plus": "chat/completions",
    "qwen3.5-plus": "chat/completions",
    "minimax-m2.5": "chat/completions",
    "minimax-m2.5-free": "chat/completions",
    "minimax-m2.7": "chat/completions",
    "glm-5.1": "chat/completions",
    "glm-5": "chat/completions",
    "kimi-k2.5": "chat/completions",
    "kimi-k2.6": "chat/completions",
    "big-pickle": "chat/completions",
    "deepseek-v4-flash-free": "chat/completions",
    "nemotron-3-super-free": "chat/completions",
    # Anthropic (messages)
    "claude-opus-4-7": "messages",
    "claude-opus-4-6": "messages",
    "claude-opus-4-5": "messages",
    "claude-opus-4-1": "messages",
    "claude-sonnet-4-6": "messages",
    "claude-sonnet-4-5": "messages",
    "claude-sonnet-4": "messages",
    "claude-haiku-4-5": "messages",
    "claude-3-5-haiku": "messages",
    # OpenAI (responses)
    "gpt-5.5": "responses",
    "gpt-5.5-pro": "responses",
    "gpt-5.4": "responses",
    "gpt-5.4-pro": "responses",
    "gpt-5.4-mini": "responses",
    "gpt-5.4-nano": "responses",
    "gpt-5.3-codex": "responses",
    "gpt-5.3-codex-spark": "responses",
    "gpt-5.2": "responses",
    "gpt-5.2-codex": "responses",
    "gpt-5.1": "responses",
    "gpt-5.1-codex": "responses",
    "gpt-5.1-codex-max": "responses",
    "gpt-5.1-codex-mini": "responses",
    "gpt-5": "responses",
    "gpt-5-codex": "responses",
    "gpt-5-nano": "responses",
    # Google Gemini
    "gemini-3.1-pro": "models/gemini-3.1-pro",
    "gemini-3-flash": "models/gemini-3-flash",
}


class OpenCodeZenClient:
    """
    Cliente para OpenCode Zen API.
    Soporta chat con modelos cloud (Claude, GPT, Gemini, Qwen, etc.)
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("OPENCODE_API_KEY", "")
        self.client = httpx.AsyncClient(timeout=300.0)
        self._available_models = []

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.api_key.startswith("sk-"))

    def _get_endpoint(self, model: str) -> str:
        """Obtiene el endpoint correcto para el modelo."""
        endpoint = MODEL_ENDPOINTS.get(model, "chat/completions")
        return f"{ZEN_BASE}/{endpoint}"

    def _build_payload(self, model: str, messages: List[Dict], **kwargs) -> Dict:
        """Construye el payload según el tipo de modelo."""
        if model.startswith("claude-"):
            # Formato Anthropic
            return {
                "model": model,
                "messages": messages,
                "max_tokens": kwargs.get("max_tokens", 4096),
            }
        elif model.startswith("gpt-"):
            # Formato OpenAI responses
            return {
                "model": model,
                "input": messages,
            }
        else:
            # Formato OpenAI-compatible (Qwen, MiniMax, GLM, etc.)
            return {
                "model": model,
                "messages": messages,
                "stream": kwargs.get("stream", False),
            }

    async def list_models(self) -> List[Dict]:
        """Lista modelos disponibles en OpenCode Zen."""
        if not self.is_configured:
            return []
        try:
            r = await self.client.get(
                f"{ZEN_BASE}/models",
                headers={"Authorization": f"Bearer {self.api_key}"}
            )
            if r.status_code == 200:
                data = r.json()
                self._available_models = data.get("data", [])
                return self._available_models
        except Exception as e:
            logger.error(f"Error listando modelos OpenCode Zen: {e}")
        return []

    async def chat(
        self,
        model: str,
        messages: List[Dict],
        stream: bool = False,
        **kwargs,
    ) -> Dict:
        """
        Chat con modelo de OpenCode Zen.
        Detecta automáticamente el endpoint correcto según el modelo.
        """
        if not self.is_configured:
            return {"error": "OpenCode Zen API key no configurada. Set OPENCODE_API_KEY env var."}

        # Resolver modelo
        model = await self._resolve_model(model)

        # Construir payload y endpoint
        endpoint = self._get_endpoint(model)
        payload = self._build_payload(model, messages, stream=stream, **kwargs)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            r = await self.client.post(endpoint, json=payload, headers=headers)
            if r.status_code == 200:
                data = r.json()
                # Normalizar respuesta a formato común
                return self._normalize_response(data, model)
            else:
                logger.error(f"OpenCode Zen API error: {r.status_code} - {r.text}")
                return {"error": f"HTTP {r.status_code}", "detail": r.text[:200]}
        except Exception as e:
            logger.error(f"OpenCode Zen chat error: {e}")
            return {"error": str(e)}

    def _normalize_response(self, data: Dict, model: str) -> Dict:
        """Normaliza respuestas de diferentes formatos a uno común."""
        # Formato OpenAI-compatible / Anthropic
        if "choices" in data:
            choice = data["choices"][0]
            content = choice.get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return {
                "content": content,
                "model": model,
                "tokens_used": usage.get("total_tokens", 0),
                "success": True,
            }
        # Formato OpenAI responses
        elif "output" in data:
            output = data["output"][0] if data.get("output") else {}
            content = output.get("content", "")
            if isinstance(content, list):
                content = content[0].get("text", "") if content else ""
            return {
                "content": content,
                "model": model,
                "success": True,
            }
        # Formato Anthropic directo
        elif "content" in data and isinstance(data["content"], list):
            content = data["content"][0].get("text", "")
            usage = data.get("usage", {})
            return {
                "content": content,
                "model": model,
                "tokens_used": usage.get("total_tokens", 0),
                "success": True,
            }
        return {"content": str(data), "model": model, "success": True}

    async def _resolve_model(self, model: str) -> str:
        """Resuelve el modelo solicitado a uno disponible."""
        # Quitar prefijo opencode/ si existe
        if model.startswith("opencode/"):
            model = model.replace("opencode/", "")

        # Si el modelo está en nuestro mapeo, usarlo directo
        if model in MODEL_ENDPOINTS:
            return model

        # Buscar coincidencia parcial
        for known in MODEL_ENDPOINTS:
            if model.lower() in known.lower() or known.lower() in model.lower():
                return known

        # Fallback al modelo por defecto
        default = ZEN_MODELS.get("chat", "qwen3.6-plus")
        logger.warning(f"OpenCode Zen: Modelo '{model}' no reconocido, usando '{default}'")
        return default

    async def close(self):
        await self.client.aclose()


# Singleton
_zen_client: Optional[OpenCodeZenClient] = None


def get_opencode_zen_client() -> OpenCodeZenClient:
    """Obtiene el cliente singleton de OpenCode Zen."""
    global _zen_client
    if _zen_client is None:
        _zen_client = OpenCodeZenClient()
        if _zen_client.is_configured:
            logger.info(f"OpenCode Zen client initialized (key: ...{_zen_client.api_key[-8:]})")
        else:
            logger.info("OpenCode Zen client initialized (no API key - set OPENCODE_API_KEY)")
    return _zen_client
