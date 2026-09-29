"""llama.cpp Provider — OpenAI-compatible /v1/chat/completions (:8080 / :8000 atomic)."""
import json
import httpx
from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall


class LlamaCppProvider(BaseProvider):
    def __init__(self, base_url: str = "http://localhost:8080", fallback_url: str | None = "http://localhost:8000",
                 timeout_s: float = 300):
        self.base_url = base_url.rstrip("/")
        self.fallback_url = fallback_url.rstrip("/") if fallback_url else None
        self._client = httpx.AsyncClient(timeout=timeout_s)

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
            "max_tokens": 1024,
        }
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
