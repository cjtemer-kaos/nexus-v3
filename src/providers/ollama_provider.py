"""Proveedor Ollama — :11434 /api/chat + /api/tags. Redirige modelo si no existe."""
import json
import logging
import httpx
from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall

logger = logging.getLogger("nexus-ollama")


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str = "http://localhost:11434", timeout_s: float = 300):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)

    async def _resolve_model(self, model: str) -> str:
        try:
            models = await self.list_models()
            if model in models:
                return model
            base = model.split(":")[0]
            for m in models:
                if m.split(":")[0] == base:
                    return m
            non_embed = [m for m in models if "embed" not in m.lower() and "nomic" not in m.lower()]
            return non_embed[0] if non_embed else (models[0] if models else model)
        except Exception:
            return model

    async def chat(self, messages, tools=None, model=None, **kw) -> LLMResponse:
        model = await self._resolve_model(model or "qwen2.5-coder:7b")
        # Ollama /api/chat expects messages with role/content; tools is OpenAI format but Ollama supports tools
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content or ""} for m in messages],
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        r = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message") or {}
        content = msg.get("content", "")
        # Tool calls in Ollama format
        tcs: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            tcs.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {}))
        # Some Ollama versions return tool_calls at top level
        if not tcs:
            for tc in data.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                tcs.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {}))
        finish = "tool_calls" if tcs else "stop"
        return LLMResponse(content=content, tool_calls=tuple(tcs), finish_reason=finish, model=model, usage={"total_tokens": data.get("eval_count", 0)})

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/api/tags")
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        r = await self._client.get(f"{self.base_url}/api/tags")
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]

    async def close(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
