"""Proveedor Ollama — :11434 /api/chat + /api/tags. Redirige modelo si no existe."""
import json
import logging
import httpx
from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall

logger = logging.getLogger("nexus-ollama")


class OllamaProvider(BaseProvider):
    def __init__(self, base_url: str = "http://localhost:11434", timeout_s: float = 300,
                 keep_alive: str = "30m", options: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_s)
        # Velocidad local: keep_alive mantiene el modelo cargado (evita recarga por request).
        self.keep_alive = keep_alive or "30m"
        self.options = dict(options or {})

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

    async def _capabilities(self, model: str) -> list[str]:
        """Capacidades del modelo (via /api/show). Cacheado por modelo."""
        try:
            m = await self._resolve_model(model or "")
        except Exception:
            m = model
        cache = getattr(self, "_caps", None)
        if cache is None:
            cache = {}
            self._caps = cache
        if m in cache:
            return cache[m]
        caps: list[str] = []
        try:
            r = await self._client.post(f"{self.base_url}/api/show", json={"model": m})
            r.raise_for_status()
            caps = list(r.json().get("capabilities") or [])
        except Exception:
            caps = []
        cache[m] = caps
        return caps

    async def supports_tools(self, model: str) -> bool:
        caps = await self._capabilities(model)
        return ("tools" in caps) if caps else True

    async def supports_vision(self, model: str) -> bool:
        caps = await self._capabilities(model)
        return ("vision" in caps) if caps else False

    async def chat(self, messages, tools=None, model=None, **kw) -> LLMResponse:
        model = await self._resolve_model(model or "qwen2.5-coder:7b")
        # Ollama /api/chat expects messages with role/content; tools is OpenAI format but Ollama supports tools
        def _wire(m):
            d = {"role": m.role, "content": m.content or ""}
            # Preservar tool_calls (assistant) y tool_call_id (tool): sin esto
            # Ollama rechaza un mensaje role=tool sin su tool_calls previo (500).
            _tcs = getattr(m, "tool_calls", ()) or ()
            if _tcs:
                d["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments or {}}}
                    for tc in _tcs
                ]
            _tcid = getattr(m, "tool_call_id", "") or ""
            if _tcid:
                d["tool_call_id"] = _tcid
            imgs = getattr(m, "images", None)
            if imgs:
                # Ollama pide base64 CRUDO — quitar el prefijo data:image/...;base64,
                d["images"] = [im.split(",", 1)[-1] if isinstance(im, str) and im.startswith("data:") else im for im in imgs]
            return d
        payload = {
            "model": model,
            "messages": [_wire(m) for m in messages],
            "stream": False,
            # P2: mantener el modelo cargado y opciones de generacion (num_ctx, temperature...)
            "keep_alive": kw.get("keep_alive") or self.keep_alive,
        }
        _opts = dict(self.options)
        if kw.get("options"):
            _opts.update(kw["options"])
        if kw.get("temperature") is not None:
            _opts["temperature"] = kw["temperature"]
        if _opts:
            payload["options"] = _opts
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
        _think = msg.get("thinking") or msg.get("reasoning_content") or msg.get("reasoning") or ""
        return LLMResponse(content=content, tool_calls=tuple(tcs), finish_reason=finish, model=model, usage={"total_tokens": data.get("eval_count", 0)}, reasoning_content=(_think or None))

    async def stream(self, messages, tools=None, model=None, **kw):
        """Streaming real via /api/chat con stream:true (deltas token a token)."""
        model = await self._resolve_model(model or "qwen2.5-coder:7b")

        def _wire(m):
            d = {"role": m.role, "content": m.content or ""}
            # Preservar tool_calls/tool_call_id (mismo motivo que en chat()).
            _tcs = getattr(m, "tool_calls", ()) or ()
            if _tcs:
                d["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments or {}}}
                    for tc in _tcs
                ]
            _tcid = getattr(m, "tool_call_id", "") or ""
            if _tcid:
                d["tool_call_id"] = _tcid
            imgs = getattr(m, "images", None)
            if imgs:
                d["images"] = [im.split(",", 1)[-1] if isinstance(im, str) and im.startswith("data:") else im for im in imgs]
            return d

        payload = {"model": model, "messages": [_wire(m) for m in messages], "stream": True,
                   "keep_alive": kw.get("keep_alive") or self.keep_alive}
        _opts = dict(self.options)
        if kw.get("options"):
            _opts.update(kw["options"])
        if kw.get("temperature") is not None:
            _opts["temperature"] = kw["temperature"]
        if _opts:
            payload["options"] = _opts
        if tools:
            payload["tools"] = tools
        content = ""
        _think = ""
        tcs: list[ToolCall] = []
        _seen: set = set()
        _usage: dict = {}
        _done_reason = None
        async with self._client.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                msg = data.get("message") or {}
                chunk = msg.get("content") or ""
                if chunk:
                    content += chunk
                    yield {"type": "text", "text": chunk}
                _tchunk = msg.get("thinking") or msg.get("reasoning_content") or ""
                if _tchunk:
                    _think += _tchunk
                    yield {"type": "reasoning", "text": _tchunk}
                for tc in msg.get("tool_calls") or []:
                    _tid = tc.get("id", "")
                    # dedup estable por contenido (el conteo cambia -> duplicaba en reenvíos)
                    _key = _tid or json.dumps(tc.get("function") or {}, sort_keys=True)
                    if _key in _seen:
                        continue
                    _seen.add(_key)
                    fn = tc.get("function") or {}
                    args = fn.get("arguments") or {}
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    tcs.append(ToolCall(id=_tid, name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {}))
                if data.get("done"):
                    _usage = {"total_tokens": (data.get("eval_count") or 0) + (data.get("prompt_eval_count") or 0)}
                    _done_reason = data.get("done_reason")
                    break
        yield {"type": "final", "response": LLMResponse(
            content=content, tool_calls=tuple(tcs),
            finish_reason=(_done_reason or ("tool_calls" if tcs else "stop")), model=model, usage=_usage,
            reasoning_content=(_think or None))}

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
