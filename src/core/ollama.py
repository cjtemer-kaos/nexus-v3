"""
Ollama Integration - LLM local para SuperNEXUS v2.0

Conexion a Ollama para generación de texto, embeddings, y routing semantico.
Sin secrets - usa Ollama local por defecto.
"""

import json
import logging
from typing import AsyncIterator, Dict, List, Optional

import httpx

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class OllamaClient:
    """
    Cliente para Ollama API local.
    Soporta chat, embeddings, y listing de modelos.
    Modo dual: si el endpoint es OpenAI-compatible (llama.cpp /v1), usa ese formato.
    """

    def __init__(self, base_url: str = "http://localhost:11434"):
        # Por defecto Ollama local (llama.cpp descartado 2026-09-28). Si el env
        # apunta a un endpoint (OLLAMA_HOST / OLLAMA_URL / LLAMACPP_URL), se respeta.
        import os
        _env = os.environ.get("LLAMACPP_URL", os.environ.get("OLLAMA_HOST", os.environ.get("OLLAMA_URL", "")))
        if _env:
            base_url = ("http://" + _env) if not _env.startswith("http") else _env
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=300.0)
        self._openai = None  # None=desconocido, True=openai-compatible, False=ollama

    async def _is_openai(self) -> bool:
        """Detecta si el endpoint es OpenAI-compatible (llama.cpp) o Ollama."""
        if self._openai is not None:
            return self._openai
        for path in ("/v1/models", "/v1/chat/completions"):
            try:
                r = await self.client.get(f"{self.base_url}{path}" if path.endswith("models") else f"{self.base_url}/v1/models", timeout=3)
                if r.status_code == 200:
                    self._openai = True
                    return True
            except Exception:
                break
        # Si responde /api/tags es Ollama
        try:
            r = await self.client.get(f"{self.base_url}/api/tags", timeout=3)
            if r.status_code == 200:
                self._openai = False
                return False
        except Exception:
            pass
        # Default: intentar openai
        self._openai = True
        return True

    async def _resolve_model(self, model: str) -> str:
        """Resuelve el modelo solicitado a uno disponible localmente en Ollama.
        En modo OpenAI-compatible (llama.cpp) devuelve el modelo tal cual."""
        if await self._is_openai():
            return model
        try:
            models = await self.list_models()
            if not models:
                return model
            
            available_names = [m.get("name", "") for m in models if m.get("name")]
            
            # 1. Coincidencia exacta
            if model in available_names:
                return model
                
            # 2. Coincidencia sin tag o con otro tag (ej: deepseek-r1:8b -> deepseek-r1:7b)
            base_requested = model.split(":")[0]
            for av in available_names:
                base_av = av.split(":")[0]
                if base_requested == base_av:
                    logger.warning(f"OllamaClient: Redireccionando modelo inexistente '{model}' -> '{av}'")
                    return av
            
            # 3. Coincidencia parcial difusa (ej: deepseek -> deepseek-r1:7b)
            for av in available_names:
                if base_requested in av or av in base_requested:
                    logger.warning(f"OllamaClient: Coincidencia difusa de modelo '{model}' -> '{av}'")
                    return av
                    
            # 4. Fallback al primer modelo no-embedding disponible (evita usar nomene/embed para chat)
            non_embed_models = [m for m in available_names if "embed" not in m.lower() and "nomic" not in m.lower()]
            if non_embed_models:
                logger.warning(f"OllamaClient: Ninguna coincidencia para '{model}'. Usando fallback: '{non_embed_models[0]}'")
                return non_embed_models[0]
                
            # 5. Fallback definitivo si no hay nada más
            return available_names[0]
        except Exception as e:
            logger.error(f"Error resolviendo modelo Ollama '{model}': {e}")
            return model

    async def chat(
        self,
        model: str,
        messages: List,
        stream: bool = False,
        options: Optional[Dict] = None,
    ) -> Dict:
        """
        Chat con modelo de Ollama.
        messages: [{"role": "user", "content": "..."}, ...]
        Normaliza respuestas de deepseek-r1: combina thinking + content.
        """
        model = await self._resolve_model(model)
        if await self._is_openai():
            # Formato OpenAI-compatible (llama.cpp)
            payload = {"model": model, "messages": messages, "stream": stream}
            if options:
                payload["temperature"] = options.get("temperature", 0.7)
                payload["max_tokens"] = options.get("num_predict", 2048)
            r = await self.client.post(f"{self.base_url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content", "")
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            if reasoning and not content:
                content = reasoning
            result: Dict = {"message": {"content": content}, "model": data.get("model", model), "done": True}
        else:
            payload = {
                "model": model,
                "messages": messages,
                "stream": stream,
            }
            if options:
                payload["options"] = options

            r = await self.client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            result = r.json()
            # Normalizar deepseek-r1: thinking field -> content
            msg = result.get("message", {})
            if msg.get("thinking"):
                content = msg.get("content", "")
                thinking = msg["thinking"]
                if not content:
                    result["message"]["content"] = thinking
                else:
                    result["message"]["content"] = f"{thinking}\n\n{content}"

        # Normalizar LFM2.5/MoE models:  thinking inline in content
        import re
        content = result.get("message", {}).get("content", "")
        if "thinking" in content:
            match = re.search(r"(?s)thinking.*?thinking(.*)", content)
            if match:
                clean = match.group(1).strip()
                if clean:
                    result["message"]["content"] = clean

        return result

    async def chat_stream(
        self,
        model: str,
        messages: List[Dict[str, str]],
        options: Optional[Dict] = None,
    ) -> AsyncIterator[str]:
        """Chat con streaming de respuesta"""
        model = await self._resolve_model(model)
        if await self._is_openai():
            # OpenAI-compatible (llama.cpp) streaming SSE
            payload = {"model": model, "messages": messages, "stream": True}
            if options:
                payload["temperature"] = options.get("temperature", 0.7)
                payload["max_tokens"] = options.get("num_predict", 2048)
            async with self.client.stream(
                "POST", f"{self.base_url}/v1/chat/completions", json=payload
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        j = json.loads(data)
                    except Exception:
                        continue
                    delta = (j.get("choices") or [{}])[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
            return
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        if options:
            payload["options"] = options

        async with self.client.stream(
            "POST", f"{self.base_url}/api/chat", json=payload
        ) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if line:
                    data = json.loads(line)
                    if data.get("done"):
                        break
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content

    async def generate(
        self,
        model: str,
        prompt: str,
        system: str = "",
        stream: bool = False,
        options: Optional[Dict] = None,
    ) -> Dict:
        """Generacion directa (sin chat history). Normaliza deepseek-r1."""
        model = await self._resolve_model(model)
        if await self._is_openai():
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.append({"role": "user", "content": prompt})
            payload = {"model": model, "messages": msgs, "stream": stream}
            if options:
                payload["temperature"] = options.get("temperature", 0.7)
                payload["max_tokens"] = options.get("num_predict", 2048)
            r = await self.client.post(f"{self.base_url}/v1/chat/completions", json=payload)
            r.raise_for_status()
            data = r.json()
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            response = msg.get("content", "")
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            if reasoning and not response:
                response = reasoning
            result: Dict = {"response": response, "model": data.get("model", model)}
        else:
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": stream,
            }
            if system:
                payload["system"] = system
            if options:
                payload["options"] = options

            r = await self.client.post(f"{self.base_url}/api/generate", json=payload)
            r.raise_for_status()
            result = r.json()
            # Normalizar deepseek-r1: response vacio + thinking lleno
            if not result.get("response") and result.get("thinking"):
                result["response"] = result["thinking"]

        # Normalizar LFM2.5/MoE:  thinking inline in response
        import re
        response = result.get("response", "")
        if " thinking" in response:
            match = re.search(r"(?s) thinking.*? thinking(.*)", response)
            if match:
                clean = match.group(1).strip()
                if clean:
                    result["response"] = clean

        return result

    async def embed(self, model: str, input: List[str]) -> List[List[float]]:
        """Genera embeddings para RAG"""
        model = await self._resolve_model(model)
        if await self._is_openai():
            payload = {"model": model, "input": input}
            r = await self.client.post(f"{self.base_url}/v1/embeddings", json=payload)
            r.raise_for_status()
            return [e.get("embedding", []) for e in r.json().get("data", [])]
        payload = {"model": model, "input": input}
        r = await self.client.post(f"{self.base_url}/api/embed", json=payload)
        r.raise_for_status()
        return r.json().get("embeddings", [])

    async def list_models(self) -> List[Dict]:
        """Lista modelos disponibles"""
        if await self._is_openai():
            r = await self.client.get(f"{self.base_url}/v1/models")
            r.raise_for_status()
            return [{"name": m.get("id", "")} for m in r.json().get("data", [])]
        r = await self.client.get(f"{self.base_url}/api/tags")
        r.raise_for_status()
        return r.json().get("models", [])

    async def auto_discover(self) -> str:
        """
        Dynamically discover active Ollama instance on local machine or standard network ports.
        Checks OLLAMA_HOST environment variable, standard port 11434, and alternative ports.
        """
        import os
        env_host = os.getenv("OLLAMA_HOST")
        if env_host:
            if not env_host.startswith("http"):
                env_host = f"http://{env_host}"
            self.base_url = env_host.rstrip("/")
            try:
                r = await self.client.get(f"{self.base_url}/api/tags", timeout=1.0)
                if r.status_code == 200:
                    logger.info(f"Ollama auto-discovered via OLLAMA_HOST: {self.base_url}")
                    return self.base_url
            except Exception:
                pass

        standard_ports = [11434, 11435, 18789, 8000]
        for port in standard_ports:
            test_url = f"http://localhost:{port}"
            try:
                r = await self.client.get(f"{test_url}/api/tags", timeout=1.0)
                if r.status_code == 200:
                    self.base_url = test_url
                    logger.info(f"Ollama auto-discovered on active port {port}: {self.base_url}")
                    return self.base_url
            except Exception:
                continue

        return self.base_url

    async def is_available(self) -> bool:
        """Verifica si el LLM esta disponible (Ollama o llama.cpp OpenAI-compatible)."""
        try:
            r = await self.client.get(f"{self.base_url}/api/tags", timeout=5)
            if r.status_code == 200:
                self._openai = False
                return True
        except Exception:
            pass
        try:
            r = await self.client.get(f"{self.base_url}/v1/models", timeout=5)
            if r.status_code == 200:
                self._openai = True
                return True
        except Exception:
            pass

        # Try auto-discovery
        original_url = self.base_url
        discovered_url = await self.auto_discover()
        if discovered_url != original_url:
            try:
                r = await self.client.get(f"{self.base_url}/api/tags", timeout=5)
                return r.status_code == 200
            except Exception:
                pass
            try:
                r = await self.client.get(f"{self.base_url}/v1/models", timeout=5)
                return r.status_code == 200
            except Exception:
                pass
        return False

    async def close(self):
        await self.client.aclose()



class LLMRouter:
    """
    Router de LLMs para SuperNEXUS.
    Selecciona modelo segun tipo de tarea.
    """

    DEFAULT_MODELS = {
        "default": "nemotron-3-nano:4b",
        "coding": "qwen2.5-coder:7b",
        "reasoning": "deepseek-r1:8b",
        "fast": "nemotron-3-nano:4b",
        "chat": "llama3.2:3b",
        "embedding": "nomic-embed-text",
    }

    def __init__(self, ollama: Optional[OllamaClient] = None, models: Optional[Dict] = None):
        self.ollama = ollama or OllamaClient()
        self.models = models or self.DEFAULT_MODELS

    def get_model_for_task(self, task: str) -> str:
        """Selecciona modelo segun tipo de tarea"""
        task_lower = task.lower()

        if any(k in task_lower for k in ["code", "program", "script", "debug", "fix"]):
            return self.models.get("coding", "qwen2.5-coder:7b")
        elif any(k in task_lower for k in ["think", "reason", "analyze", "plan", "design"]):
            return self.models.get("reasoning", "deepseek-r1:7b")
        elif any(k in task_lower for k in ["quick", "simple", "fast", "summary"]):
            return self.models.get("fast", "nemotron-3-nano:4b")
        else:
            return self.models.get("default", "llama3.2")

    async def classify_intent(self, query: str) -> Dict:
        """Clasifica intencion del usuario usando LLM"""
        prompt = f"""Classify this query into one category: code, research, debug, creative, devops, security, general.
Query: {query}
Return only the category name."""

        response = await self.ollama.generate(
            model=self.models.get("fast", "nemotron-3-nano:4b"),
            prompt=prompt,
        )
        category = response.get("response", "general").strip().lower()
        return {"category": category, "confidence": 0.8}

    async def route_task(self, task: str) -> Dict:
        """Rutea tarea al modelo y gema apropiado"""
        intent = await self.classify_intent(task)
        model = self.get_model_for_task(task)

        gem_mapping = {
            "code": "coder",
            "research": "scholar",
            "debug": "debugger",
            "creative": "creative",
            "devops": "devops",
            "security": "security",
            "general": "director",
        }

        return {
            "model": model,
            "gem": gem_mapping.get(intent["category"], "director"),
            "intent": intent,
        }

    async def generate_response(
        self,
        task: str,
        context: str = "",
        stream: bool = False,
    ) -> Dict:
        """Genera respuesta completa para una tarea"""
        routing = await self.route_task(task)
        model = routing["model"]

        system_prompt = f"""You are NEXUS IA v2.0.
You are acting as the {routing['gem']} gem.
Be concise, helpful, and accurate in Spanish.
If you don't know something, say so."""

        messages = [
            {"role": "system", "content": system_prompt},
        ]

        if context:
            messages.append({"role": "user", "content": f"Context:\n{context}"})

        messages.append({"role": "user", "content": task})

        if stream:
            return {"stream": True, "model": model, "routing": routing}
        else:
            response = await self.ollama.chat(model=model, messages=messages)
            return {
                "reply": response.get("message", {}).get("content", ""),
                "model": model,
                "routing": routing,
                "done": response.get("done", False),
            }
