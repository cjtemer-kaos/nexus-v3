"""Proveedor Nube — compatible OpenAI (Zen, Anthropic via adapter, etc.)."""
import json
import os
import re
import time
import uuid
import logging
import httpx
from pathlib import Path
from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse, ToolCall

logger = logging.getLogger("nexus-cloud")
_FREE_RE = re.compile(r"free", re.I)


class CloudProvider(BaseProvider):
    def __init__(self, base_url: str, api_key: str | None = None, auth_style: str = "bearer",
                 timeout_s: float = 60, strict_tools: bool = False, session_id: str | None = None,
                 model_exclude: list[str] | None = None):
        self.base_url = base_url.rstrip("/")
        # P3 (pi/constrained sampling): strict tool schemas (OpenAI) -> tool calls
        # validos al 1er intento, menos reintentos. Opt-in (algunos endpoints no lo soportan).
        self.strict_tools = bool(strict_tools)
        # Expand ${OPENCODE_API_KEY} style if leaked from config.yaml parsing
        raw_key = api_key or os.environ.get("OPENCODE_API_KEY", "")
        if raw_key and raw_key.startswith("${"):
            m = re.match(r"\$\{([^:}]+)(?::-[^}]+)?\}", raw_key)
            if m:
                raw_key = os.environ.get(m.group(1), "")
        self.api_key = raw_key
        self.auth_style = auth_style
        # opencode Go EXIGE x-opencode-session (id estable por conversacion): sin el,
        # devuelve 400 MissingSessionID. Default estable por instancia; se puede
        # overridear por llamada con kw["session_id"] (id de la sesion de chat).
        self.session_id = session_id or uuid.uuid4().hex
        # Prefijos de modelos a OCULTAR (no van por /v1/chat/completions: p.ej. en opencode-go
        # los de /messages o /responses dan 400 -> mejor no listarlos en la UI).
        self.model_exclude = [e.lower() for e in (model_exclude or [])]
        self._client = httpx.AsyncClient(timeout=timeout_s)
        # Cache for free-model discovery (mem + file)
        self._free_cache: list[str] | None = None
        self._free_cache_ts: float = 0.0
        self._free_ttl_s: float = 300.0  # 5 min — Zen cambia gratis varias veces por semana

    def _headers(self, session_id: str | None = None) -> dict:
        # Headers tipo navegador: Cloudflare (Zen) bloquea clientes "desnudos" con 403.
        base = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Origin": "https://opencode.ai",
            "Referer": "https://opencode.ai/",
            # opencode Go: sin x-opencode-session -> 400 MissingSessionID (routing/caching).
            "x-opencode-session": session_id or self.session_id,
        }
        if not self.api_key:
            return base
        if self.auth_style == "x-api-key":
            return {**base, "x-api-key": self.api_key}
        return {**base, "Authorization": f"Bearer {self.api_key}"}

    def _raise_http(self, r) -> None:
        """Como raise_for_status pero incluye el CUERPO de la respuesta.
        opencode-go devuelve 400/403 con JSON util (p.ej. MissingSessionID); sin el cuerpo
        el error es opaco ('400 Bad Request')."""
        if r.status_code >= 400:
            body = ""
            try:
                body = (r.text or "")[:600]
            except Exception:
                pass
            url = str(getattr(getattr(r, "request", None), "url", self.base_url))
            raise RuntimeError(f"HTTP {r.status_code} en {url}: {body}")

    @staticmethod
    def _strict_tools(tools: list[dict]) -> list[dict]:
        """Marca los tools como strict (OpenAI), recursivo: additionalProperties=false +
        required=all properties en CADA objeto anidado (OpenAI strict lo exige)."""
        def _strict_obj(schema):
            if not isinstance(schema, dict):
                return
            if schema.get("type") == "object" or "properties" in schema:
                schema["additionalProperties"] = False
                props = schema.get("properties") or {}
                if props:
                    schema["required"] = list(props.keys())
                for v in props.values():
                    _strict_obj(v)
            if isinstance(schema.get("items"), dict):
                _strict_obj(schema["items"])
            for key in ("anyOf", "oneOf", "allOf"):
                for sub in schema.get(key) or []:
                    _strict_obj(sub)

        out: list[dict] = []
        for t in tools:
            try:
                t2 = json.loads(json.dumps(t))
            except Exception:
                out.append(t)
                continue
            fn = t2.get("function") or {}
            params = fn.get("parameters")
            if isinstance(params, dict):
                _strict_obj(params)
                fn["strict"] = True  # solo si hay schema de objeto valido
            t2["function"] = fn
            out.append(t2)
        return out

    async def chat(self, messages, tools=None, model=None, **kw) -> LLMResponse:
        if not self.api_key:
            # Still try; server may allow anonymous
            pass
        def _to_wire(m):
            # Formato OpenAI completo: sin tool_calls/tool_call_id el modelo
            # no enlaza resultados y repite llamadas / rompe la conversación (400).
            d: dict = {"role": m.role, "content": m.content or ""}
            imgs = getattr(m, "images", None)
            if imgs and m.role == "user" and m.content is not None:
                # Adjuntos visuales (DECK): content parts estilo OpenAI vision.
                parts: list[dict] = [{"type": "text", "text": m.content or ""}]
                for im in imgs:
                    url = im if isinstance(im, str) and im.startswith("data:") else f"data:image/png;base64,{im}"
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                d["content"] = parts
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
            "model": model or "nemotron-3-ultra-free",
            "messages": [_to_wire(m) for m in messages],
        }
        if kw.get("max_tokens"):
            payload["max_tokens"] = kw["max_tokens"]
        if kw.get("temperature") is not None:
            payload["temperature"] = kw["temperature"]
        if kw.get("options"):
            payload.update(kw["options"])
        if tools:
            payload["tools"] = self._strict_tools(tools) if self.strict_tools else tools
            payload["tool_choice"] = "required" if kw.get("force_tools") else kw.get("tool_choice", "auto")
        r = await self._client.post(f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers(kw.get("session_id")))
        self._raise_http(r)
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
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
        usage = data.get("usage") or {}
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        return LLMResponse(content=msg.get("content"), tool_calls=tuple(tcs),
                           finish_reason=choice.get("finish_reason", "stop"), model=model or "", usage=usage, reasoning_content=reasoning)

    async def health(self) -> bool:
        try:
            r = await self._client.get(f"{self.base_url}/v1/models", headers=self._headers())
            return r.status_code == 200
        except Exception:
            return False

    async def list_models(self) -> list[str]:
        r = await self._client.get(f"{self.base_url}/v1/models", headers=self._headers())
        self._raise_http(r)
        ids = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
        if self.model_exclude:
            ids = [mid for mid in ids if not any(mid.lower().startswith(p) for p in self.model_exclude)]
        return ids

    async def probe_free_models_health(self, models: list[str], timeout_s: float = 12.0) -> dict[str, dict]:
        """Prueba real por modelo con mini chat (max_tokens=5). Solo 400 'Model is unavailable'
        es baja permanente (deepseek off). 429/500/503/timeout son transitorios → NO filtrar,
        se mantienen como ok=True con warning. Cache 60s para transitorios, 5m para 200/400.
        """
        # cache en memoria por modelo
        if not hasattr(self, "_health_cache"):
            self._health_cache: dict[str, tuple[float, dict]] = {}  # type: ignore
        now = time.monotonic()
        out: dict[str, dict] = {}
        to_probe: list[str] = []
        for mid in models:
            cached = self._health_cache.get(mid)  # type: ignore
            if cached:
                age = now - cached[0]
                # TTL diferenciado: 400 y 200 aguantan 5m, transitorios solo 60s
                st = cached[1]
                ttl = 60.0 if st.get("status") in (429, 500, 502, 503, 0) and st.get("ok") else self._free_ttl_s
                if age < ttl:
                    out[mid] = cached[1]
                    continue
            to_probe.append(mid)
        if to_probe:
            import asyncio as _asyncio
            sem = _asyncio.Semaphore(3)  # no saturar Zen
            async def _one(mid: str):
                async with sem:
                    payload = {"model": mid, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
                    try:
                        # cliente efímero con timeout corto para no bloquear health
                        async with httpx.AsyncClient(timeout=timeout_s) as cli:
                            r = await cli.post(f"{self.base_url}/v1/chat/completions", json=payload, headers=self._headers())
                            if r.status_code == 200:
                                res = {"ok": True, "status": 200, "error": None}
                            elif r.status_code == 400 and "unavailable" in r.text.lower():
                                # único caso permanente: deepseek "Model is unavailable"
                                try:
                                    j = r.json()
                                    msg = (j.get("error") or {}).get("message") or r.text[:200]
                                except Exception:
                                    msg = r.text[:200]
                                res = {"ok": False, "status": 400, "error": str(msg)[:220]}
                            elif r.status_code in (429, 500, 502, 503):
                                # transitorio: existe, solo saturado — NO filtrar
                                res = {"ok": True, "status": r.status_code, "error": f"transient {r.status_code}: {r.text[:120]}"}
                            elif r.status_code == 403:
                                # gating por modelo/plan de Zen: persistente (verificado 2026-09-28:
                                # nemotron/mimo/muse/jev/ling/longcat dan 403 fijo; space-bunny 200)
                                res = {"ok": False, "status": 403, "error": f"forbidden (model/plan gating): {r.text[:150] or 'empty body'}"}
                            else:
                                try:
                                    j = r.json()
                                    msg = (j.get("error") or {}).get("message") or j.get("error") or r.text[:200]
                                except Exception:
                                    msg = r.text[:200]
                                # otros 4xx distintos de unavailable -> transitorio también
                                is_perm = r.status_code == 400 and "unavailable" in str(msg).lower()
                                res = {"ok": not is_perm, "status": r.status_code, "error": str(msg)[:220] if not is_perm else str(msg)[:220]}
                                if is_perm:
                                    res["ok"] = False
                    except Exception as e:
                        # timeout / red -> transitorio, no filtrar (muse te responde pero el probe puede timear)
                        res = {"ok": True, "status": 0, "error": f"transient timeout: {str(e)[:180]}"}
                    self._health_cache[mid] = (time.monotonic(), res)  # type: ignore
                    return mid, res
            results = await _asyncio.gather(*[_one(m) for m in to_probe])
            for mid, res in results:
                out[mid] = res
        return out

    async def list_free_models(self, force: bool = False, verify: bool = False) -> list[str]:
        """Descubre modelos gratis en vivo (id contiene 'free'). Auto-TTL 5 min + cache en disco.
        verify=True → hace probe real y filtra offline (deepseek etc). Nunca rompe: fallback a cache.
        """
        now = time.monotonic()
        if not force and self._free_cache is not None and (now - self._free_cache_ts) < self._free_ttl_s:
            return list(self._free_cache)
        # file cache path (best-effort)
        cache_file = Path("data/cloud_free_cache.json")
        try:
            ids = await self.list_models()
            free_raw = [mid for mid in ids if _FREE_RE.search(mid)]
            free_raw = sorted(set(free_raw))
            free = free_raw
            # persistir RAW siempre (Zen dice que son free) — no perdemos mimo/ling si están 429/503
            if free_raw:
                self._free_cache = list(free_raw)
                self._free_cache_ts = now
                try:
                    cache_file.parent.mkdir(parents=True, exist_ok=True)
                    # preservar verified/verified_ts si existen (el raw write NO debe pisarlos)
                    _prev: dict = {}
                    if cache_file.exists():
                        try:
                            _prev = json.loads(cache_file.read_text())
                            if not isinstance(_prev, dict):
                                _prev = {}
                        except Exception:
                            _prev = {}
                    _prev.update({"ts": time.time(), "models": free_raw, "raw": free_raw})
                    cache_file.write_text(json.dumps(_prev, ensure_ascii=False))
                except Exception:
                    pass
                logger.info(f"Zen free discovery raw: {free_raw}")
            if verify and free_raw:
                # filtrado real: solo baja permanente (400 unavailable) sale; 429/500/timeout quedan
                health = await self.probe_free_models_health(free_raw)
                # ok=False solo si 400 unavailable → filtrar. ok=True (incl. transitorios) → mantener
                verified = [mid for mid, st in health.items() if st.get("ok") is not False]
                # si por algún motivo todos dieron 400, no dejar vacío: devolver raw
                if not verified:
                    logger.warning(f"Zen free verify todos 400, devuelvo raw {free_raw} health={health}")
                    free = list(free_raw)
                else:
                    # priorizar los 200 arriba, pero mantener transitorios (muse con 500 sigue viva)
                    verified_sorted = sorted(verified, key=lambda m: (0 if health[m].get("status")==200 else 1, m))
                    free = verified_sorted
                    dropped = [m for m in free_raw if m not in verified]
                    if dropped:
                        logger.info(f"Zen free dropped permanentes: {dropped} health={ {k:health[k] for k in dropped}}")
                    logger.info(f"Zen free verified (solo 400 filtrado): {free} / raw {sorted(health.keys())}")
                    # persistir verified en disco para filtrar registros sin verify
                    try:
                        _d = json.loads(cache_file.read_text())
                        _d["verified"] = list(free)
                        _d["verified_ts"] = time.time()
                        cache_file.write_text(json.dumps(_d, ensure_ascii=False))
                    except Exception:
                        pass
                # NO pisar cache RAW con verified; retornar verified directo (next call sin verify vuelve a raw)
                logger.info(f"Zen free discovery verified: {free}")
                return list(free)
            if free_raw:
                logger.info(f"Zen free discovery: {free_raw}")
                return list(free_raw)
        except Exception as e:
            logger.debug(f"list_free_models live failed: {e}")
        # fallback: memory then disk
        if self._free_cache is not None:
            return list(self._free_cache)
        try:
            if cache_file.exists():
                data = json.loads(cache_file.read_text())
                disk = data.get("models") or []
                if disk:
                    self._free_cache = list(disk)
                    self._free_cache_ts = now
                    return list(disk)
        except Exception:
            pass
        return []

    async def close(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
