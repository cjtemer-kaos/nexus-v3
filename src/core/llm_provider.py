"""
LLM Provider - Pasarela unificada de inferencia multi-proveedor

Unifica todas las inferencias bajo un formato estandar (OpenAI-compatible).
Detecta automaticamente si redirige a Ollama (local) o APIs cloud.
Formatea mensajes de forma transparente segun el proveedor.

Patrones integrados:
- CredentialPool failover (multi-endpoint con retry)
- SchemaSanitizer (JSON Schema 2020-12 → OpenAPI 3.0)
- SequenceScrubber (turn alignment para Gemini/Ollama)
- Multi-Endpoint Failover (Ollama → Cloud automatico)

Proveedores soportados:
- Ollama (local, prioritario)
- OpenAI / OpenRouter (cloud)
- Anthropic (cloud)
- Google Gemini (cloud)
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx

from src.core.credential_pool import CredentialPool
from src.core.schema_sanitizer import SchemaSanitizer
from src.core.session_manager import SequenceScrubber

logger = logging.getLogger("nexus-llm-provider")


@dataclass
class LLMResponse:
    """Respuesta estandarizada de cualquier proveedor"""
    content: str
    model: str
    provider: str
    tokens_used: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    tokens_cached: int = 0  # cache hits (anthropic/openai support this)
    cost_usd: float = 0.0   # computed from MODEL_PRICING; 0.0 for local/unknown
    duration_ms: float = 0
    finish_reason: str = "stop"
    raw_response: Optional[Dict] = None


# Public per-1M-token pricing (input / output, USD). Local models = 0.
# Order matters for prefix matching: more specific first.
# Source: provider public pricing pages, refresh as needed.
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    # OpenAI
    "gpt-5":         {"in": 1.25,  "out": 10.0},
    "gpt-4.1":       {"in": 2.0,   "out": 8.0},
    "gpt-4o":        {"in": 2.5,   "out": 10.0},
    "gpt-4o-mini":   {"in": 0.15,  "out": 0.6},
    "o1":            {"in": 15.0,  "out": 60.0},
    "o3":            {"in": 10.0,  "out": 40.0},
    # Anthropic
    "claude-opus":   {"in": 15.0,  "out": 75.0},
    "claude-sonnet": {"in": 3.0,   "out": 15.0},
    "claude-haiku":  {"in": 0.8,   "out": 4.0},
    # Google
    "gemini-2.5-pro":   {"in": 1.25, "out": 10.0},
    "gemini-2.5-flash": {"in": 0.075, "out": 0.3},
    # OpenRouter free tier sentinels — keep at 0 to not double-count.
    "ollama":        {"in": 0.0, "out": 0.0},
    "local":         {"in": 0.0, "out": 0.0},
}


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Best-effort cost estimate. Returns 0.0 for unknown / local models —
    never raises. Prefix match so 'gpt-4o-2024-08-06' matches 'gpt-4o'."""
    if not model:
        return 0.0
    ml = model.lower()
    pricing = None
    # Most specific prefix wins (longer key first).
    for key in sorted(MODEL_PRICING, key=len, reverse=True):
        if key in ml:
            pricing = MODEL_PRICING[key]
            break
    if not pricing:
        return 0.0
    return round(
        (prompt_tokens / 1_000_000.0) * pricing["in"]
        + (completion_tokens / 1_000_000.0) * pricing["out"],
        6,
    )


@dataclass
class LLMRequest:
    """Peticion estandarizada para cualquier proveedor"""
    messages: List[Dict[str, str]]
    model: str
    provider: str = "ollama"
    system_prompt: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    top_p: float = 1.0
    tools: Optional[List[Dict]] = None
    tool_choice: str = "auto"
    stop_sequences: Optional[List[str]] = None
    timeout_seconds: float = 120
    images: Optional[List[str]] = None  # Base64 o URLs


class LLMProvider:
    """
    Pasarela unificada de inferencia multi-proveedor.

    Uso:
        provider = LLMProvider()
        response = await provider.chat(
            messages=[{"role": "user", "content": "Hola"}],
            model="qwen2.5-coder:7b",
            provider="ollama",
        )
    """

    # Endpoints por proveedor
    PROVIDER_ENDPOINTS = {
        "ollama": {
            "base_url": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            "chat_path": "/api/chat",
            "generate_path": "/api/generate",
        },
        "openai": {
            "base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com"),
            "chat_path": "/v1/chat/completions",
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api",
            "chat_path": "/v1/chat/completions",
        },
        "anthropic": {
            "base_url": "https://api.anthropic.com",
            "chat_path": "/v1/messages",
        },
        "gemini": {
            "base_url": "https://generativelanguage.googleapis.com",
            "chat_path": "/v1beta/openai/chat/completions",  # OpenAI-compatible
        },
    }

    def __init__(self, credential_pool: CredentialPool = None):
        self.credential_pool = credential_pool or CredentialPool()
        self._client = httpx.AsyncClient(timeout=120.0)
        self._stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "total_tokens": 0,
            "failovers": 0,
            "provider_stats": {},
        }

    async def chat(self, request: LLMRequest) -> LLMResponse:
        """
        Envio principal con failover automatico.

        Si el proveedor falla, intenta failover en orden:
        Ollama → OpenRouter → Gemini → Anthropic
        """
        start = time.time()
        self._stats["total_requests"] += 1

        # Preparar mensajes con SequenceScrubber
        scrubbed_messages = SequenceScrubber.scrub(request.messages)

        # Si hay system prompt, insertarlo al inicio
        if request.system_prompt:
            if not scrubbed_messages or scrubbed_messages[0].get("role") != "system":
                scrubbed_messages.insert(0, {"role": "system", "content": request.system_prompt})

        # Sanitizar schemas de herramientas si hay
        tools = request.tools
        if tools:
            tools = SchemaSanitizer.sanitize_tool_definitions(tools, provider=request.provider)

        # Intentar con el proveedor principal
        last_error = None
        providers_to_try = self._get_failover_chain(request.provider)

        # Snapshot original (human-only) for tool-state revalidation on failover.
        # Pattern from openakita: when we switch provider, the new model inherits
        # tool_use/tool_result chains from a different model that may have stale
        # assumptions about browser state, MCP server lists, desktop windows, etc.
        # Strip stateful tool chains and inject a barrier asking for re-check.
        messages_for_provider = scrubbed_messages

        for attempt_idx, provider in enumerate(providers_to_try):
            if attempt_idx > 0:
                messages_for_provider = self._inject_tool_state_barrier(scrubbed_messages)
                logger.info(
                    f"Failover to {provider}: tool-state barrier injected "
                    f"({len(scrubbed_messages)} -> {len(messages_for_provider)} msgs)"
                )
                try:
                    from src.observability.event_stream import emit, EventType
                    emit(EventType.LLM_FAILOVER,
                         data={"from": providers_to_try[attempt_idx - 1],
                               "to": provider, "attempt": attempt_idx},
                         source="llm_provider")
                except Exception:
                    pass
            try:
                response = await self._call_provider(
                    provider=provider,
                    messages=messages_for_provider,
                    model=request.model,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    top_p=request.top_p,
                    tools=tools,
                    tool_choice=request.tool_choice,
                    stop_sequences=request.stop_sequences,
                    timeout_seconds=request.timeout_seconds,
                    images=request.images,
                )

                response.duration_ms = (time.time() - start) * 1000
                response.provider = provider

                # Cost estimate (best-effort; 0.0 for local/unknown models).
                # Use prompt/completion split when available; fall back to total.
                try:
                    pt = response.tokens_prompt or 0
                    ct = response.tokens_completion or 0
                    if not (pt or ct) and response.tokens_used:
                        # provider didn't split — attribute everything to output as upper bound
                        ct = response.tokens_used
                    response.cost_usd = estimate_cost_usd(response.model or "", pt, ct)
                except Exception:
                    response.cost_usd = 0.0

                self._stats["successful_requests"] += 1
                self._stats["total_tokens"] += response.tokens_used
                self._stats.setdefault("total_cost_usd", 0.0)
                self._stats["total_cost_usd"] = round(
                    self._stats["total_cost_usd"] + response.cost_usd, 6
                )
                self._update_provider_stats(provider, True)

                # Emit LLM_TOKEN_USAGE for observability (best-effort). Read
                # session_id and request_id from contextvars so budget_tracker
                # can attribute cost to the right session without changing the
                # LLMProvider call signature.
                try:
                    from src.observability.event_stream import emit, EventType
                    sid = rid = None
                    try:
                        from src.observability.context import current_session_id, current_request_id
                        sid = current_session_id()
                        rid = current_request_id()
                    except Exception:
                        pass
                    emit(EventType.LLM_TOKEN_USAGE,
                         data={
                             "provider": provider, "model": response.model,
                             "prompt_tokens": response.tokens_prompt,
                             "completion_tokens": response.tokens_completion,
                             "cached_tokens": response.tokens_cached,
                             "total_tokens": response.tokens_used,
                             "cost_usd": response.cost_usd,
                             "duration_ms": round(response.duration_ms, 1),
                         },
                         session_id=sid, request_id=rid,
                         source="llm_provider")
                except Exception:
                    pass

                if provider != request.provider:
                    self._stats["failovers"] += 1
                    logger.info(f"Failover: {request.provider} → {provider}")

                return response

            except Exception as e:
                last_error = e
                self._update_provider_stats(provider, False)
                logger.warning(f"Provider {provider} failed: {e}")

                # Marcar credencial en cooldown si aplica
                self.credential_pool.mark_error(provider, str(e))

        # Todos los proveedores fallaron
        self._stats["failed_requests"] += 1
        raise RuntimeError(
            f"All providers failed. Last error: {last_error}. "
            f"Attempted: {providers_to_try}"
        )

    async def _call_provider(
        self,
        provider: str,
        messages: List[Dict],
        model: str,
        temperature: float,
        max_tokens: int,
        top_p: float,
        tools: Optional[List[Dict]],
        tool_choice: str,
        stop_sequences: Optional[List[str]],
        timeout_seconds: float,
        images: Optional[List[str]],
    ) -> LLMResponse:
        """Llama a un proveedor especifico"""
        if provider == "ollama":
            return await self._call_ollama(messages, model, temperature, max_tokens, top_p, tools, timeout_seconds, images)
        elif provider in ("openai", "openrouter"):
            return await self._call_openai_compatible(provider, messages, model, temperature, max_tokens, top_p, tools, tool_choice, stop_sequences, timeout_seconds)
        elif provider == "anthropic":
            return await self._call_anthropic(messages, model, temperature, max_tokens, tools, stop_sequences, timeout_seconds)
        elif provider == "gemini":
            return await self._call_gemini(messages, model, temperature, max_tokens, tools, timeout_seconds)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    async def _call_ollama(
        self,
        messages: List[Dict],
        model: str,
        temperature: float,
        max_tokens: int,
        top_p: float,
        tools: Optional[List[Dict]],
        timeout_seconds: float,
        images: Optional[List[str]],
    ) -> LLMResponse:
        """Llama a Ollama local"""
        endpoint = self.PROVIDER_ENDPOINTS["ollama"]
        url = f"{endpoint['base_url']}{endpoint['chat_path']}"

        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "top_p": top_p,
            },
        }

        if tools:
            payload["tools"] = tools

        # Agregar imagenes al ultimo mensaje user
        if images:
            last_user_msg = None
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user_msg = msg
                    break
            if last_user_msg:
                content = last_user_msg.get("content", "")
                last_user_msg["images"] = images

        response = await self._client.post(url, json=payload, timeout=timeout_seconds)
        # Retry on 502 (Ollama model loading — transient)
        if response.status_code == 502:
            logger.warning(f"Ollama 502 for {model}, retrying in 3s...")
            await asyncio.sleep(3)
            response = await self._client.post(url, json=payload, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json()

        message = data.get("message", {})
        content = message.get("content", "")

        return LLMResponse(
            content=content,
            model=model,
            provider="ollama",
            tokens_used=data.get("eval_count", 0) + data.get("prompt_eval_count", 0),
            tokens_prompt=data.get("prompt_eval_count", 0),
            tokens_completion=data.get("eval_count", 0),
            raw_response=data,
        )

    async def _call_openai_compatible(
        self,
        provider: str,
        messages: List[Dict],
        model: str,
        temperature: float,
        max_tokens: int,
        top_p: float,
        tools: Optional[List[Dict]],
        tool_choice: str,
        stop_sequences: Optional[List[str]],
        timeout_seconds: float,
    ) -> LLMResponse:
        """Llama a OpenAI o OpenRouter"""
        endpoint = self.PROVIDER_ENDPOINTS[provider]
        url = f"{endpoint['base_url']}{endpoint['chat_path']}"

        # Obtener API key del credential pool
        cred = self.credential_pool.get_credential(provider)
        api_key = cred.key if cred else os.environ.get(f"{provider.upper()}_API_KEY", "")

        if not api_key:
            raise ValueError(f"No API key available for {provider}")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://supernexus.local"
            headers["X-Title"] = "SuperNEXUS v2"

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": top_p,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        if stop_sequences:
            payload["stop"] = stop_sequences

        response = await self._client.post(url, json=payload, headers=headers, timeout=timeout_seconds)

        if response.status_code == 429:
            self.credential_pool.mark_cooldown(cred, 429)
            raise RuntimeError(f"Rate limited by {provider}")
        elif response.status_code == 401:
            self.credential_pool.mark_cooldown(cred, 401)
            raise RuntimeError(f"Auth failed for {provider}")

        response.raise_for_status()
        data = response.json()

        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = data.get("usage", {})

        return LLMResponse(
            content=message.get("content", ""),
            model=model,
            provider=provider,
            tokens_used=usage.get("total_tokens", 0),
            tokens_prompt=usage.get("prompt_tokens", 0),
            tokens_completion=usage.get("completion_tokens", 0),
            finish_reason=choice.get("finish_reason", "stop"),
            raw_response=data,
        )

    async def _call_anthropic(
        self,
        messages: List[Dict],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict]],
        stop_sequences: Optional[List[str]],
        timeout_seconds: float,
    ) -> LLMResponse:
        """Llama a Anthropic Claude"""
        endpoint = self.PROVIDER_ENDPOINTS["anthropic"]
        url = f"{endpoint['base_url']}{endpoint['chat_path']}"

        cred = self.credential_pool.get_credential("anthropic")
        api_key = cred.key if cred else os.environ.get("ANTHROPIC_API_KEY", "")

        if not api_key:
            raise ValueError("No API key available for anthropic")

        # Anthropic requiere system prompt separado
        system_prompt = ""
        filtered_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = msg.get("content", "")
            else:
                filtered_messages.append(msg)

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

        payload = {
            "model": model,
            "messages": filtered_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        if system_prompt:
            payload["system"] = system_prompt

        if tools:
            # Anthropic usa formato diferente para herramientas
            payload["tools"] = [
                {
                    "name": t.get("function", {}).get("name", ""),
                    "description": t.get("function", {}).get("description", ""),
                    "input_schema": SchemaSanitizer.sanitize(
                        t.get("function", {}).get("parameters", {}),
                        provider="anthropic",
                    ),
                }
                for t in tools
            ]

        if stop_sequences:
            payload["stop_sequences"] = stop_sequences

        response = await self._client.post(url, json=payload, headers=headers, timeout=timeout_seconds)

        if response.status_code == 429:
            self.credential_pool.mark_cooldown(cred, 429)
            raise RuntimeError("Rate limited by Anthropic")

        response.raise_for_status()
        data = response.json()

        content = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block.get("text", "")

        usage = data.get("usage", {})

        return LLMResponse(
            content=content,
            model=model,
            provider="anthropic",
            tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            tokens_prompt=usage.get("input_tokens", 0),
            tokens_completion=usage.get("output_tokens", 0),
            raw_response=data,
        )

    async def _call_gemini(
        self,
        messages: List[Dict],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict]],
        timeout_seconds: float,
    ) -> LLMResponse:
        """Llama a Google Gemini via OpenAI-compatible endpoint"""
        return await self._call_openai_compatible(
            provider="gemini",
            messages=messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            top_p=1.0,
            tools=tools,
            tool_choice="auto",
            stop_sequences=None,
            timeout_seconds=timeout_seconds,
        )

    def _get_failover_chain(self, primary_provider: str) -> List[str]:
        """Retorna cadena de failover comenzando por el proveedor principal"""
        chain = [primary_provider]
        failover_order = ["ollama", "openrouter", "gemini", "anthropic", "openai"]

        for p in failover_order:
            if p != primary_provider and p not in chain:
                chain.append(p)

        return chain

    def _inject_tool_state_barrier(self, messages: List[Dict]) -> List[Dict]:
        """
        Tool-state revalidation barrier (openakita pattern).

        On provider failover, the next model inherits tool_use / tool_result
        chains from a different model. Those chains carry implicit assumptions
        about browser state, MCP server availability, desktop windows, etc.
        that the new model has no reason to trust.

        Strategy: keep only system + plain user/assistant text messages; drop
        prior tool chains entirely; append a single barrier user-message that
        names the stateful tools and forces the new model to re-check before
        using them.
        """
        if not messages:
            return messages

        clean: List[Dict] = []
        had_tool_chain = False
        for m in messages:
            role = m.get("role")
            content = m.get("content")

            # Strip any tool_use / tool_result content blocks; if the message
            # becomes empty after stripping, drop it.
            if isinstance(content, list):
                kept_blocks = [
                    b for b in content
                    if not (isinstance(b, dict)
                            and b.get("type") in ("tool_use", "tool_result"))
                ]
                if len(kept_blocks) != len(content):
                    had_tool_chain = True
                if not kept_blocks:
                    continue
                clean.append({**m, "content": kept_blocks})
                continue

            if role in ("system", "user", "assistant"):
                clean.append(m)

        if not had_tool_chain:
            return messages

        barrier = {
            "role": "user",
            "content": (
                "[provider-failover] The previous model lost its connection. "
                "Any browser session, MCP server list, desktop window, file "
                "handle, or other stateful tool context from earlier in this "
                "conversation MUST be treated as unknown. Before using such "
                "tools again, re-check state explicitly (e.g. browser_open, "
                "list_mcp_servers, desktop_inspect). Continue from where the "
                "user left off."
            ),
        }
        clean.append(barrier)
        return clean

    def _update_provider_stats(self, provider: str, success: bool):
        """Actualiza estadisticas por proveedor"""
        if provider not in self._stats["provider_stats"]:
            self._stats["provider_stats"][provider] = {"success": 0, "failed": 0}

        if success:
            self._stats["provider_stats"][provider]["success"] += 1
        else:
            self._stats["provider_stats"][provider]["failed"] += 1

    def get_stats(self) -> Dict:
        return self._stats.copy()

    async def close(self):
        await self._client.aclose()
