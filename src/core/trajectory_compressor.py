"""
TrajectoryCompressor - F1: Context Compression con LLM

Comprime el historial de conversacion usando Ollama local para generar
resumenes inteligentes del medio de la conversacion, protegiendo el inicio
(system, primer user) y el final (ultimos N mensajes).

Simplificado para SuperNEXUS v2: usa Ollama local + estimacion de tokens.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Any

logger = logging.getLogger("nexus-compressor")

SUMMARIZATION_PROMPT = """Summarize the following agent conversation turns concisely. This summary will replace these turns in the conversation history.

Write the summary from a neutral perspective describing what the assistant did and learned. Include:
1. What actions the assistant took (tool calls, searches, file operations)
2. Key information or results obtained
3. Any important decisions or findings
4. Relevant data, file names, values, or outputs

Keep the summary factual and informative. Target approximately 200-400 tokens.

---
TURNS TO SUMMARIZE:
{content}
---

Write only the summary, starting with "[CONTEXT SUMMARY]:" prefix."""


@dataclass
class CompressionMetrics:
    original_tokens: int = 0
    compressed_tokens: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 1.0
    original_turns: int = 0
    compressed_turns: int = 0
    was_compressed: bool = False
    summarization_api_calls: int = 0
    summarization_errors: int = 0


class TrajectoryCompressor:
    """
    Comprime trayectorias de sesion usando Ollama local para resumenes LLM.
    
    Estrategia:
    1. Proteger primeros turnos (system, primer user, primer assistant)
    2. Proteger ultimos N turnos
    3. Comprimir el medio con resumen LLM
    4. Reemplazar region comprimida con un solo mensaje de resumen
    """

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        summarization_model: str = "qwen2.5:0.5b",
        target_max_tokens: int = 8000,
        protect_last_n: int = 4,
        max_retries: int = 2,
        timeout_seconds: float = 30.0,
    ):
        self.ollama_url = ollama_url
        self.summarization_model = summarization_model
        self.target_max_tokens = target_max_tokens
        self.protect_last_n = protect_last_n
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self._available = None

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Estimacion rapida de tokens (len/4)"""
        if not text:
            return 0
        return len(text) // 4

    async def is_available(self) -> bool:
        """Verifica si Ollama esta disponible"""
        if self._available is not None:
            return self._available
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/tags")
                self._available = resp.status_code == 200
        except Exception:
            self._available = False
        return self._available

    async def generate_summary(self, content: str, metrics: CompressionMetrics) -> str:
        """Genera resumen via Ollama local"""
        prompt = SUMMARIZATION_PROMPT.format(content=content[:4000])

        for attempt in range(self.max_retries):
            try:
                metrics.summarization_api_calls += 1
                import httpx
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(
                        f"{self.ollama_url}/api/generate",
                        json={
                            "model": self.summarization_model,
                            "prompt": prompt,
                            "stream": False,
                            "options": {"temperature": 0.3, "num_predict": 1000},
                        },
                    )
                    if resp.status_code == 200:
                        summary = resp.json().get("response", "").strip()
                        if summary.startswith("[CONTEXT SUMMARY]:"):
                            return summary
                        return f"[CONTEXT SUMMARY]: {summary}"
                    else:
                        logger.warning(f"Ollama summary failed: HTTP {resp.status_code}")
            except Exception as e:
                metrics.summarization_errors += 1
                logger.warning(f"Summary attempt {attempt + 1} failed: {e}")

        # Fallback: resumen basico
        return (
            "[CONTEXT SUMMARY]: Previous turns contained tool calls and responses "
            "that have been compressed to save context space. Key actions and results are preserved."
        )

    def find_compressible_region(self, messages: List[Any]) -> tuple:
        """
        Encuentra indices protegidos y region comprimible.
        
        Returns: (head_indices, compressible_start, compressible_end, tail_start_idx)
        """
        n = len(messages)
        protected = set()

        # Proteger system y primer user
        first_system = first_user = first_assistant = None
        for i, msg in enumerate(messages):
            role = getattr(msg, "role", msg.get("role", "")) if hasattr(msg, "role") or isinstance(msg, dict) else ""
            if role == "system" and first_system is None:
                first_system = i
            elif role in ("user", "human") and first_user is None:
                first_user = i
            elif role in ("assistant", "gpt") and first_assistant is None:
                first_assistant = i

        if first_system is not None:
            protected.add(first_system)
        if first_user is not None:
            protected.add(first_user)
        if first_assistant is not None:
            protected.add(first_assistant)

        # Proteger ultimos N turnos
        for i in range(max(0, n - self.protect_last_n), n):
            protected.add(i)

        # Determinar region comprimible
        head_indices = sorted([i for i in protected if i < n // 2])
        tail_indices = sorted([i for i in protected if i >= n // 2])

        compressible_start = max(head_indices) + 1 if head_indices else 0
        compressible_end = min(tail_indices) if tail_indices else n

        return head_indices, compressible_start, compressible_end

    def extract_content_for_summary(self, messages: List[Any], start: int, end: int) -> str:
        """Extrae contenido de turnos para resumir"""
        parts = []
        for i in range(start, min(end, len(messages))):
            msg = messages[i]
            role = getattr(msg, "role", msg.get("role", "unknown"))
            content = getattr(msg, "content", msg.get("content", ""))

            if len(content) > 2000:
                content = content[:1000] + "\n...[truncated]...\n" + content[-300:]

            parts.append(f"[Turn {i} - {role.upper()}]:\n{content}")

        return "\n\n".join(parts)

    async def compress(self, messages: List[Any], summary_text: str = "") -> Dict:
        """
        Comprime una lista de mensajes.
        
        Args:
            messages: Lista de SessionMessage o dicts con role/content
            summary_text: Resumen pre-generado (si ya se tiene)
            
        Returns:
            Dict con mensajes comprimidos y metricas
        """
        metrics = CompressionMetrics()
        metrics.original_turns = len(messages)
        metrics.original_tokens = sum(
            getattr(m, "tokens", m.get("tokens", 0)) or self.estimate_tokens(getattr(m, "content", m.get("content", "")))
            for m in messages
        )

        n = len(messages)
        if n <= self.protect_last_n + 2:
            return {
                "messages": messages,
                "status": "skipped_too_short",
                "metrics": metrics,
            }

        head_indices, compressible_start, compressible_end = self.find_compressible_region(messages)

        middle_messages = messages[compressible_start:compressible_end]
        if not middle_messages:
            return {
                "messages": messages,
                "status": "skipped_no_middle",
                "metrics": metrics,
            }

        # Generar resumen
        if not summary_text:
            if await self.is_available():
                content = self.extract_content_for_summary(messages, compressible_start, compressible_end)
                summary_text = await self.generate_summary(content, metrics)
            else:
                # Fallback sin LLM
                tool_calls = sum(1 for m in middle_messages if "tool" in str(getattr(m, "role", m.get("role", ""))).lower() or "execute" in str(getattr(m, "content", m.get("content", ""))).lower())
                summary_text = (
                    f"[RESUMEN DE CONTEXTO INTERMEDIO]: Se comprimieron {len(middle_messages)} mensajes "
                    f"medios para liberar espacio de tokens. Durante esta fase se interactuo en "
                    f"{tool_calls} ocasiones con herramientas de sistema."
                )

        # Construir lista compacta
        head_messages = [messages[i] for i in head_indices]
        tail_messages = messages[compressible_end:]

        # Crear mensaje de resumen
        try:
            from src.core.session_manager import SessionMessage
            summary_msg = SessionMessage(
                role="system",
                content=summary_text,
                tokens=self.estimate_tokens(summary_text),
                model="trajectory_compressor",
            )
        except ImportError:
            summary_msg = {
                "role": "system",
                "content": summary_text,
                "tokens": self.estimate_tokens(summary_text),
                "model": "trajectory_compressor",
            }

        new_messages = head_messages + [summary_msg] + tail_messages

        metrics.compressed_turns = len(new_messages)
        metrics.compressed_tokens = sum(
            getattr(m, "tokens", m.get("tokens", 0)) or self.estimate_tokens(getattr(m, "content", m.get("content", "")))
            for m in new_messages
        )
        metrics.tokens_saved = metrics.original_tokens - metrics.compressed_tokens
        metrics.compression_ratio = metrics.compressed_tokens / max(metrics.original_tokens, 1)
        metrics.was_compressed = True

        return {
            "messages": new_messages,
            "status": "success",
            "metrics": metrics,
            "summary": summary_text,
        }

    def get_status(self) -> Dict:
        return {
            "available": self._available,
            "model": self.summarization_model,
            "ollama_url": self.ollama_url,
            "target_max_tokens": self.target_max_tokens,
            "protect_last_n": self.protect_last_n,
        }
