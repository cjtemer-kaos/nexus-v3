"""llama.cpp Provider — OpenAI-compatible /v1/chat/completions (:8080 / :8000 atomic)."""
import json
import httpx
from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall


class LlamaCppProvider(BaseProvider):
    def __init__(self, base_url: str = "http://localhost:8080", fallback_url: str | None = "http://localhost:8000",
                 timeout_s: float = 300, max_tokens: int = 2048, options: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.fallback_url = fallback_url.rstrip("/") if fallback_url else None
        self._client = httpx.AsyncClient(timeout=timeout_s)
        # Velocidad local (pi): cache_prompt reutiliza el KV cache del prefijo comun;
        # max_tokens configurable (antes fijo 1024, corto para tool-calls largos).
        self.max_tokens = int(max_tokens or 2048)
        self.options = dict(options or {})

    async def chat(self, messages, tools=None, model=None, force_tools: bool = False, **kw) -> LLMResponse:
        # Auto-carga: si el modelo está en el catálogo local, asegurar que esté
        # cargado en este puerto antes de pedir (swap bajo demanda + touch anti-idle).
        try:
            from urllib.parse import urlparse as _up
            from src.providers import llama_manager as _lm
            _port = _up(self.base_url).port or 8080
            _mid = model or "nexus-director-v6"
            if _lm.model_entry(_mid):
                _lm.touch(_port)
                if not _lm.model_path(_mid):
                    pass
                else:
                    import asyncio as _aio
                    ok = await _aio.to_thread(_lm.ensure_model, _mid, _port, 180)
                    if not ok:
                        pass
        except Exception:
            pass
        def _to_wire(m):
            # Formato OpenAI completo: sin tool_calls/tool_call_id el modelo
            # no enlaza resultados y repite llamadas (doom-loop).
            d: dict = {"role": m.role, "content": m.content or ""}
            tcs = getattr(m, "tool_calls", ()) or ()
            if m.role == "assistant" and tcs:
                wire = []
                for i, tc in enumerate(tcs):
                    _args = getattr(tc, "arguments", {})
                    if isinstance(_args, dict):
                        _args = json.dumps(_args, ensure_ascii=False)
                    wire.append({
                        "id": getattr(tc, "id", "") or f"call-{i}",
                        "type": "function",
                        "function": {"name": getattr(tc, "name", ""), "arguments": _args or "{}"},
                    })
                d["tool_calls"] = wire
            _tcid = getattr(m, "tool_call_id", "")
            if m.role == "tool" and _tcid:
                d["tool_call_id"] = _tcid
            return d

        payload: dict = {
            "model": model or "nexus-director-v6",
            "messages": [_to_wire(m) for m in messages],
            "max_tokens": kw.get("max_tokens") or self.max_tokens,
            # pi/llama.cpp: reutiliza el prompt/KV cache del prefijo estable (system+tools).
            "cache_prompt": True,
        }
        _opts = dict(self.options)
        if kw.get("options"):
            _opts.update(kw["options"])
        if kw.get("temperature") is not None:
            _opts["temperature"] = kw["temperature"]
        payload.update(_opts)
        if tools:
            # NOTA: sin grammar ni response_format a propósito:
            # - llama.cpp rechaza grammar+tools juntos (400)
            # - response_format dejaba colgado/vacío a omnicoder
            # Con tool_choice required los modelos capaces emiten tool_calls reales;
            # el extractor del loop rescata JSON escrito en texto como respaldo.
            payload["tools"] = tools
            payload["tool_choice"] = "required" if force_tools else "auto"
        last_err: Exception | None = None
        for base in [self.base_url] + ([self.fallback_url] if self.fallback_url else []):
            try:
                r = await self._client.post(f"{base}/v1/chat/completions", json=payload)
                try:
                    from urllib.parse import urlparse as _up2
                    from src.providers import llama_manager as _lm2
                    _lm2.touch(_up2(base).port or 8080)
                except Exception:
                    pass
                r.raise_for_status()
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                content = msg.get("content")
                reasoning = msg.get("reasoning_content") or msg.get("reasoning")
                tcs: list[ToolCall] = []
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args) if args else {}
                        except Exception:
                            args = {}
                    tcs.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {}))
                # Fallback: modelos locales escriben <tool_call>{"name":"..","arguments":{}}</tool_call>
                # en content o reasoning_content en vez de tool_calls OpenAI — extraerlos aquí
                # para que has_tool_calls sea True y el loop no descarte el razonamiento.
                if not tcs:
                    for _src in (content or "", reasoning or ""):
                        if not _src or "<tool_call" not in _src:
                            continue
                        import re as _re_llama
                        for _m in _re_llama.finditer(r"<tool_call[^>]*>\s*(\{.*?\})\s*</tool_call>", _src, flags=_re_llama.DOTALL):
                            try:
                                _obj = json.loads(_m.group(1))
                                _name = str(_obj.get("name", "")).strip()
                                _args = _obj.get("arguments", {})
                                if isinstance(_args, str):
                                    try: _args = json.loads(_args) if _args else {}
                                    except Exception: _args = {}
                                if _name and isinstance(_args, dict):
                                    tcs.append(ToolCall(id=f"llama-{len(tcs)}", name=_name, arguments=_args))
                            except Exception:
                                continue
                        if tcs:
                            break
                usage = data.get("usage") or {}
                return LLMResponse(content=content, tool_calls=tuple(tcs),
                                   finish_reason=choice.get("finish_reason", "stop"), model=model or "", usage=usage, reasoning_content=reasoning)
            except Exception as e:
                last_err = e
                continue
        raise RuntimeError(f"chat llama.cpp falló en todos los endpoints: {last_err}")

    async def stream(self, messages, tools=None, model=None, force_tools: bool = False, **kw):
        """Streaming real via /v1/chat/completions con stream:true (SSE OpenAI)."""
        def _to_wire(m):
            d: dict = {"role": m.role, "content": m.content or ""}
            tcs = getattr(m, "tool_calls", ()) or ()
            if m.role == "assistant" and tcs:
                wire = []
                for i, tc in enumerate(tcs):
                    _args = getattr(tc, "arguments", {})
                    if isinstance(_args, dict):
                        _args = json.dumps(_args, ensure_ascii=False)
                    wire.append({"id": getattr(tc, "id", "") or f"call-{i}", "type": "function",
                                 "function": {"name": getattr(tc, "name", ""), "arguments": _args or "{}"}})
                d["tool_calls"] = wire
            _tcid = getattr(m, "tool_call_id", "")
            if m.role == "tool" and _tcid:
                d["tool_call_id"] = _tcid
            return d

        payload: dict = {"model": model or "nexus-director-v6", "messages": [_to_wire(m) for m in messages],
                         "max_tokens": kw.get("max_tokens") or self.max_tokens, "cache_prompt": True, "stream": True}
        _opts = dict(self.options)
        if kw.get("options"):
            _opts.update(kw["options"])
        if kw.get("temperature") is not None:
            _opts["temperature"] = kw["temperature"]
        payload.update(_opts)
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "required" if force_tools else "auto"
        # Auto-carga del modelo (igual que chat()): sin esto el stream falla si no está cargado.
        try:
            from urllib.parse import urlparse as _up
            from src.providers import llama_manager as _lm
            _port = _up(self.base_url).port or 8080
            _mid = model or "nexus-director-v6"
            if _lm.model_entry(_mid) and _lm.model_path(_mid):
                import asyncio as _aio
                await _aio.to_thread(_lm.ensure_model, _mid, _port, 180)
                _lm.touch(_port)
        except Exception:
            pass

        content = ""
        reasoning = ""
        acc: dict[str, dict] = {}
        _finish = None
        _bases = [self.base_url] + ([self.fallback_url] if self.fallback_url else [])
        last_err: Exception | None = None
        for _base in _bases:
            try:
                async with self._client.stream("POST", f"{_base}/v1/chat/completions", json=payload) as r:
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_s = line[5:].strip()
                        if data_s == "[DONE]":
                            break
                        try:
                            data = json.loads(data_s)
                        except Exception:
                            continue
                        choice = (data.get("choices") or [{}])[0]
                        if choice.get("finish_reason"):
                            _finish = choice.get("finish_reason")
                        delta = choice.get("delta") or {}
                        c = delta.get("content") or ""
                        if c:
                            content += c
                            yield {"type": "text", "text": c}
                        _rc = delta.get("reasoning_content") or delta.get("reasoning") or ""
                        if _rc:
                            reasoning += _rc
                        for tc in delta.get("tool_calls") or []:
                            # Sin "index" (o null) los deltas colapsaban en la ranura 0: usar id/count.
                            _ix = tc.get("index")
                            idx = str(_ix) if _ix is not None else str(tc.get("id") or f"_a{len(acc)}")
                            slot = acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                            if tc.get("id"):
                                slot["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            _a = fn.get("arguments")
                            if isinstance(_a, str):
                                slot["args"] += _a
                            elif _a is not None:
                                slot["args"] += json.dumps(_a, ensure_ascii=False)
                try:
                    from urllib.parse import urlparse as _up2
                    from src.providers import llama_manager as _lm2
                    _lm2.touch(_up2(_base).port or 8080)
                except Exception:
                    pass
                last_err = None
                break
            except Exception as e:
                last_err = e
                if content or acc or reasoning:  # ya emitimos tokens: no reintentar en otro endpoint
                    raise
                continue
        if last_err is not None:
            raise last_err
        tcs: list[ToolCall] = []
        for idx in sorted(acc):
            slot = acc[idx]
            args = slot["args"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args else {}
                except Exception:
                    args = {}
            if slot["name"]:
                tcs.append(ToolCall(id=slot["id"] or f"llama-{idx}", name=slot["name"],
                                    arguments=args if isinstance(args, dict) else {}))
        # Fallback: modelos locales escriben <tool_call>{"name":..,"arguments":{}}</tool_call>
        # en el content/reasoning (igual que chat()); sin esto el streaming perderia la tool-call.
        if not tcs and "<tool_call" in (content + reasoning):
            import re as _re_llama
            for _src in (content, reasoning):
                for _m in _re_llama.finditer(r"<tool_call[^>]*>\s*(\{.*?\})\s*</tool_call>", _src, flags=_re_llama.DOTALL):
                    try:
                        _obj = json.loads(_m.group(1))
                        _name = str(_obj.get("name", "")).strip()
                        _args = _obj.get("arguments", {})
                        if isinstance(_args, str):
                            try:
                                _args = json.loads(_args) if _args else {}
                            except Exception:
                                _args = {}
                        if _name and isinstance(_args, dict):
                            tcs.append(ToolCall(id=f"llama-{len(tcs)}", name=_name, arguments=_args))
                    except Exception:
                        continue
                if tcs:
                    break
        yield {"type": "final", "response": LLMResponse(content=content, tool_calls=tuple(tcs),
              finish_reason=(_finish or ("tool_calls" if tcs else "stop")), model=model or "",
              reasoning_content=reasoning or None)}

    async def health(self) -> bool:
        for base in [self.base_url] + ([self.fallback_url] if self.fallback_url else []):
            try:
                r = await self._client.get(f"{base}/health")
                if r.status_code == 200:
                    self.base_url = base  # stick to working endpoint
                    return True
                # Some llama.cpp versions expose /v1/models as health
                if r.status_code in (404, 405):
                    r2 = await self._client.get(f"{base}/v1/models")
                    if r2.status_code == 200:
                        self.base_url = base
                        return True
            except Exception:
                continue
        return False

    async def list_models(self) -> list[str]:
        for base in [self.base_url] + ([self.fallback_url] if self.fallback_url else []):
            try:
                r = await self._client.get(f"{base}/v1/models")
                r.raise_for_status()
                return [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
            except Exception:
                continue
        return []

    async def close(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
