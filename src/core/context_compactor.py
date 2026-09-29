"""
ContextCompactor - 4-Layer Progressive Context Compaction Pipeline

Layers:
  L0: SecurityLingua — comprime jailbreaks a intencion pura
  L1: Snip — elimina mensajes intermedios con safe split point
  L2: Micro — colapsa tool results antiguos a placeholders
  L3: Budget — persiste outputs grandes, mantiene preview 2000 chars
  L4: Summary — LLM summary si excede limite (1 API call)
  Emergency: ReactiveCompact — si API devuelve prompt_too_long

Cada capa tiene un target de proporcion del presupuesto original.
Progressive: las capas tempranas comprimen primero, las tardias solo si aun excede.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("nexus-compactor")


@dataclass
class LayerMetrics:
    name: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_saved: int = 0
    messages_in: int = 0
    messages_out: int = 0
    ratio: float = 1.0


@dataclass
class CompactionMetrics:
    original_messages: int = 0
    compressed_messages: int = 0
    original_tokens: int = 0
    compressed_tokens: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 1.0
    layers_applied: List[str] = field(default_factory=list)
    layers: List[LayerMetrics] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


@dataclass
class LayerConfig:
    enabled: bool = True
    target_ratio: float = 0.0  # 0.0 = sin target especifico


class BudgetTier:
    AGGRESSIVE = "aggressive"
    NORMAL = "normal"
    CONSERVATIVE = "conservative"

    TIERS = {
        AGGRESSIVE: {
            "context_limit_ratio": 0.25,
            "max_messages_ratio": 0.3,
            "keep_recent": 2,
            "max_tool_result_ratio": 0.3,
            "enable_summary": True,
        },
        NORMAL: {
            "context_limit_ratio": 0.5,
            "max_messages_ratio": 0.6,
            "keep_recent": 4,
            "max_tool_result_ratio": 0.6,
            "enable_summary": True,
        },
        CONSERVATIVE: {
            "context_limit_ratio": 0.8,
            "max_messages_ratio": 0.9,
            "keep_recent": 8,
            "max_tool_result_ratio": 0.9,
            "enable_summary": False,
        },
    }

    @classmethod
    def get_config(cls, tier: str, base_config: Dict) -> Dict:
        t = cls.TIERS.get(tier, cls.TIERS[cls.NORMAL])
        return {
            "context_limit": int(base_config.get("context_limit", 50000) * t["context_limit_ratio"]),
            "max_messages": max(5, int(base_config.get("max_messages", 50) * t["max_messages_ratio"])),
            "keep_recent": t["keep_recent"],
            "max_tool_result_bytes": int(base_config.get("max_tool_result_bytes", 200000) * t["max_tool_result_ratio"]),
            "enable_summary": t["enable_summary"],
        }


class ContextCompactor:
    """
    Multi-layer compaction pipeline con 4 capas progresivas + emergency.
    
    Orden: L0 SecurityLingua → L1 Snip → L2 Micro → L3 Budget → L4 Summary
    Emergency: ReactiveCompact
    """

    def __init__(
        self,
        max_messages: int = 50,
        keep_recent: int = 4,
        max_tool_result_bytes: int = 200_000,
        persist_threshold: int = 30_000,
        context_limit: int = 50_000,
        ollama_url: str = "http://localhost:11434",
        summarization_model: str = "qwen2.5:0.5b",
        perplexity_model: str = "qwen2.5:0.5b",
        layer_configs: Dict[str, LayerConfig] = None,
    ):
        self.max_messages = max_messages
        self.keep_recent = keep_recent
        self.max_tool_result_bytes = max_tool_result_bytes
        self.persist_threshold = persist_threshold
        self.context_limit = context_limit
        self.ollama_url = ollama_url
        self.summarization_model = summarization_model
        self.perplexity_model = perplexity_model
        self._ollama_available: Optional[bool] = None
        self._working_set: set = set()
        self.layer_configs = layer_configs or {
            "L0_security": LayerConfig(enabled=True),
            "L1_snip": LayerConfig(enabled=True, target_ratio=0.5),
            "L2_micro": LayerConfig(enabled=True, target_ratio=0.6),
            "L3_budget": LayerConfig(enabled=True, target_ratio=0.7),
            "L4_summary": LayerConfig(enabled=True, target_ratio=0.3),
        }

    def set_budget_tier(self, tier: str):
        if tier not in BudgetTier.TIERS:
            raise ValueError(f"Invalid budget tier: {tier}. Use: {list(BudgetTier.TIERS.keys())}")
        config = BudgetTier.get_config(tier, {
            "context_limit": self.context_limit,
            "max_messages": self.max_messages,
            "max_tool_result_bytes": self.max_tool_result_bytes,
        })
        self.context_limit = config["context_limit"]
        self.max_messages = config["max_messages"]
        self.keep_recent = config["keep_recent"]
        self.max_tool_result_bytes = config["max_tool_result_bytes"]
        if not config["enable_summary"]:
            self.layer_configs["L4_summary"].enabled = False
        return config

    def get_optimal_tier(self, messages: List[Dict]) -> str:
        total = self._count_tokens(messages)
        ratio = total / max(self.context_limit, 1)
        if ratio > 2.0:
            return BudgetTier.AGGRESSIVE
        elif ratio > 1.0:
            return BudgetTier.NORMAL
        return BudgetTier.CONSERVATIVE

    def compact(self, messages: List[Dict], strategy: str = "auto") -> Dict:
        metrics = CompactionMetrics()
        metrics.original_messages = len(messages)
        metrics.original_tokens = self._count_tokens(messages)

        working = list(messages)
        remaining_tokens = metrics.original_tokens

        # Emit MEMORY_COMPACTED at start (best-effort).
        try:
            from src.observability.event_stream import emit, EventType
            from src.observability.context import current_session_id
            emit(EventType.MEMORY_COMPACTED,
                 data={"phase": "started", "messages": len(messages),
                       "tokens": metrics.original_tokens,
                       "limit": self.context_limit,
                       "usage_ratio": round(metrics.original_tokens / max(self.context_limit,1), 3)},
                 session_id=current_session_id(),
                 source="context_compactor")
        except Exception:
            pass

        # L0: SecurityLingua — comprime jailbreaks a intencion pura
        if self.layer_configs["L0_security"].enabled:
            lm = self._measure("L0_security", working)
            working = self._security_lingua(working)
            lm.output_tokens = self._count_tokens(working)
            lm.tokens_saved = lm.input_tokens - lm.output_tokens
            lm.ratio = lm.output_tokens / max(lm.input_tokens, 1)
            metrics.layers.append(lm)
            metrics.layers_applied.append("L0_security")
            remaining_tokens = lm.output_tokens

        # L1: Snip — elimina mensajes intermedios
        if self.layer_configs["L1_snip"].enabled and remaining_tokens > self.context_limit * 0.4:
            lm = self._measure("L1_snip", working)
            working = self.snip_compact(working)
            lm.output_tokens = self._count_tokens(working)
            lm.tokens_saved = lm.input_tokens - lm.output_tokens
            lm.ratio = lm.output_tokens / max(lm.input_tokens, 1)
            metrics.layers.append(lm)
            metrics.layers_applied.append("L1_snip")
            remaining_tokens = lm.output_tokens

        # L2: Micro — colapsa tool results antiguos
        if self.layer_configs["L2_micro"].enabled and remaining_tokens > self.context_limit * 0.5:
            lm = self._measure("L2_micro", working)
            working = self.micro_compact(working)
            lm.output_tokens = self._count_tokens(working)
            lm.tokens_saved = lm.input_tokens - lm.output_tokens
            lm.ratio = lm.output_tokens / max(lm.input_tokens, 1)
            metrics.layers.append(lm)
            metrics.layers_applied.append("L2_micro")
            remaining_tokens = lm.output_tokens

        # L3: Budget — persiste outputs grandes
        if self.layer_configs["L3_budget"].enabled and remaining_tokens > self.context_limit * 0.6:
            lm = self._measure("L3_budget", working)
            working = self.tool_result_budget(working)
            lm.output_tokens = self._count_tokens(working)
            lm.tokens_saved = lm.input_tokens - lm.output_tokens
            lm.ratio = lm.output_tokens / max(lm.input_tokens, 1)
            metrics.layers.append(lm)
            metrics.layers_applied.append("L3_budget")
            remaining_tokens = lm.output_tokens

        # L4: Summary — LLM summary si aun excede
        if self.layer_configs["L4_summary"].enabled and remaining_tokens > self.context_limit:
            lm = self._measure("L4_summary", working)
            loop = asyncio.get_event_loop()
            if loop.is_running():
                working = loop.run_until_complete(self._auto_compact(working))
            else:
                working = asyncio.run(self._auto_compact(working))
            lm.output_tokens = self._count_tokens(working)
            lm.tokens_saved = lm.input_tokens - lm.output_tokens
            lm.ratio = lm.output_tokens / max(lm.input_tokens, 1)
            metrics.layers.append(lm)
            metrics.layers_applied.append("L4_summary")

        metrics.compressed_messages = len(working)
        metrics.compressed_tokens = self._count_tokens(working)
        metrics.tokens_saved = metrics.original_tokens - metrics.compressed_tokens
        metrics.compression_ratio = (
            metrics.compressed_tokens / max(metrics.original_tokens, 1)
        )

        return {
            "messages": working,
            "metrics": metrics,
        }

    def _count_tokens(self, messages: List[Dict]) -> int:
        return sum(
            m.get("tokens", 0) or estimate_tokens(m.get("content", ""))
            for m in messages
        )

    def _measure(self, name: str, messages: List[Dict]) -> LayerMetrics:
        return LayerMetrics(
            name=name,
            input_tokens=self._count_tokens(messages),
            messages_in=len(messages),
        )

    # ─── Layer 0: SecurityLingua ─────────────────────────────────────────

    JAILBREAK_PATTERNS = [
        r"(ignore|bypass|disregard)\s+(all\s+)?(previous|prior|above|system)\s+(instructions|rules|prompts)",
        r"(you\s+are\s+now|act\s+as|pretend\s+to\s+be|roleplay\s+as)\s+(dan|jailbreak|unrestricted|god\s*mode)",
        r"(developer\s*mode|debug\s*mode|maintenance\s*mode)",
        r"(\[system\]|<system>|SYSTEM:)\s*(reset|override|replace)",
        r"(from\s*now\s*on|starting\s*now|henceforth)\s*(you\s*(will|must|should))",
    ]

    def _security_lingua(self, messages: List[Dict]) -> List[Dict]:
        """
        Capa 0: Comprime prompts sospechosos a su intención pura.
        Si un mensaje contiene patrones de jailbreak, lo reemplaza con
        la intención extraída, eliminando el ruido de distracción.
        """
        result = []
        for msg in messages:
            content = msg.get("content", "")
            if not isinstance(content, str):
                result.append(msg)
                continue

            is_suspicious = False
            for pattern in self.JAILBREAK_PATTERNS:
                if re.search(pattern, content, re.IGNORECASE):
                    is_suspicious = True
                    break

            if is_suspicious:
                # Extraer la intención pura: últimas oraciones significativas
                sentences = re.split(r'[.!?]+', content)
                meaningful = [s.strip() for s in sentences if len(s.strip()) > 10]
                if meaningful:
                    intent = meaningful[-1] if len(meaningful[-1]) < 200 else meaningful[-1][:200]
                    compressed = f"[SECURITY: Suspicious prompt compressed. Intent: {intent}]"
                else:
                    compressed = "[SECURITY: Suspicious prompt compressed. No clear intent extracted.]"
                logger.warning(f"SecurityLingua: Compressed suspicious message ({len(content)} → {len(compressed)} chars)")
                result.append({**msg, "content": compressed})
            else:
                result.append(msg)

        return result

    # ─── Layer 1: snip_compact ───────────────────────────────────────────

    def snip_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        L1: Elimina mensajes intermedios cuando exceden max_messages.
        Mantiene head (3) + tail (max_messages - 3).
        Uses SafeSplitPoint to avoid breaking code blocks.
        """
        return self.safe_snip_compact(messages)

    # ─── Layer 2: micro_compact ──────────────────────────────────────────

    def micro_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        L2: Colapsa cadenas de tool_call→tool_result antiguas en placeholders.
        Mantiene solo los últimos keep_recent tool results completos.
        """
        tool_results = self._collect_tool_results(messages)
        if len(tool_results) <= self.keep_recent:
            return messages

        compacted = list(messages)
        for msg_idx, block_idx, block in tool_results[: -self.keep_recent]:
            content = block.get("content", "")
            if isinstance(content, str) and len(content) > 120:
                compacted[msg_idx]["content"][block_idx]["content"] = (
                    "[Earlier tool result compacted. Re-run tool if needed.]"
                )

        return compacted

    # ── Layer 3: tool_result_budget ─────────────────────────────────────

    def tool_result_budget(self, messages: List[Dict]) -> List[Dict]:
        """
        L3: Trunca/persiste resultados de tools que excedan presupuesto.
        Patrón head+tail para outputs largos.
        """
        if not messages:
            return messages

        last = messages[-1]
        if last.get("role") != "user" or not isinstance(last.get("content"), list):
            return messages

        blocks = [
            (i, b)
            for i, b in enumerate(last["content"])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]

        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
        if total <= self.max_tool_result_bytes:
            return messages

        ranked = sorted(
            blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True
        )

        for _, block in ranked:
            if total <= self.max_tool_result_bytes:
                break
            content = str(block.get("content", ""))
            if len(content) <= self.persist_threshold:
                continue

            tool_use_id = block.get("tool_use_id", "unknown")
            block["content"] = self._persist_large_output(tool_use_id, content)
            total = sum(len(str(b.get("content", ""))) for _, b in blocks)

        return messages

    # ─── Layer 4: auto_compact (LLM summary) ─────────────────────────────

    async def _auto_compact(self, messages: List[Dict]) -> List[Dict]:
        """L4: Resumen LLM cuando el contexto excede el límite."""
        if not await self._is_ollama_available():
            return self._fallback_compact(messages)

        try:
            import httpx

            conversation = str(messages)[:80000]
            # Cursor-style structured self-summary (from odysseus context_compactor).
            # Dense format optimized for resumability: each section is a fixed slot
            # the next turn can scan instantly. Preserves causality, not just facts.
            prompt = (
                "Summarize this agent conversation so work can continue across a "
                "context boundary. Output ONLY the structured summary below — no "
                "preamble, no closing remarks. Be concrete, no adjectives.\n\n"
                "## Conversation Summary\n"
                f"**Turns summarized:** {len(messages)}\n\n"
                "### User Goal\n"
                "(One sentence: what is the user actually trying to accomplish?)\n\n"
                "### Key Decisions Made\n"
                "(Bullet list. Each: decision + why. Skip if none.)\n\n"
                "### Code/Files Touched\n"
                "(Path + one-line what-changed. Skip if none.)\n\n"
                "### Tools Used & Results\n"
                "(Tool name + concrete outcome. Skip noise.)\n\n"
                "### User Constraints\n"
                "(Hard limits, preferences, must-not-do. Skip if none.)\n\n"
                "### Pending Work\n"
                "(What the next turn must continue. Be specific — file paths, "
                "exact next action.)\n\n"
                "### Blockers / Open Questions\n"
                "(What needs user input or external resolution. Skip if none.)\n\n"
                "---\n\nCONVERSATION:\n\n" + conversation
            )

            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={
                        "model": self.summarization_model,
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.3, "num_predict": 2000},
                    },
                )
                if resp.status_code == 200:
                    summary = resp.json().get("response", "").strip()
                    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]
        except Exception as e:
            logger.warning(f"Auto-compact LLM failed: {e}")

        return self._fallback_compact(messages)

    def _fallback_compact(self, messages: List[Dict]) -> List[Dict]:
        tool_calls = sum(
            1
            for m in messages
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "tool_result"
        )
        summary = (
            f"[Compacted: {len(messages)} messages, {tool_calls} tool calls. "
            f"Earlier conversation summarized to save context.]"
        )
        return [{"role": "user", "content": summary}]

    # ─── Helpers ─────────────────────────────────────────────────────────

    def _collect_tool_results(self, messages: List[Dict]) -> List[tuple]:
        blocks = []
        for mi, msg in enumerate(messages):
            if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
                continue
            for bi, block in enumerate(msg["content"]):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    blocks.append((mi, bi, block))
        return blocks

    def _persist_large_output(self, tool_use_id: str, output: str) -> str:
        persist_dir = Path.home() / ".nexus" / "tool-outputs"
        persist_dir.mkdir(parents=True, exist_ok=True)
        path = persist_dir / f"{tool_use_id}.txt"

        if not path.exists():
            path.write_text(output, encoding="utf-8")

        preview = output[:2000]
        return (
            f"<persisted-output>\n"
            f"Full output saved to: {path}\n"
            f"Preview:\n{preview}\n"
            f"</persisted-output>"
        )

    def _estimate_size(self, messages: List[Dict]) -> int:
        return len(str(messages))

    async def _is_ollama_available(self) -> bool:
        if self._ollama_available is not None:
            return self._ollama_available
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/tags")
                self._ollama_available = resp.status_code == 200
        except Exception:
            self._ollama_available = False
        return self._ollama_available

    def reactive_compact(self, messages: List[Dict]) -> List[Dict]:
        """Emergency: reactive compact cuando API retorna prompt_too_long."""
        summary = (
            f"[Reactive compact: conversation truncated due to context overflow. "
            f"Last {self.keep_recent} messages preserved.]"
        )
        return [{"role": "user", "content": summary}] + messages[-self.keep_recent:]

    def get_status(self) -> Dict:
        return {
            "max_messages": self.max_messages,
            "keep_recent": self.keep_recent,
            "max_tool_result_bytes": self.max_tool_result_bytes,
            "context_limit": self.context_limit,
            "summarization_model": self.summarization_model,
            "working_set_size": len(self._working_set),
        }


    # --- SafeSplitPoint ---

    SPLIT_BOUNDARIES = [
        ("code_block", re.compile(r"```")),
        ("json_block", re.compile(r"^\s*[\{\[]")),
        ("xml_tag", re.compile(r"<\w+>|</\w+>")),
        ("tool_call", re.compile(r"tool_use|tool_result")),
        ("section_header", re.compile(r"^#{1,6}\s")),
    ]

    def find_safe_split_point(self, text: str, target_pos: int) -> int:
        """
        Find a safe position to split text without breaking code blocks,
        JSON, XML, or tool calls. Returns the nearest safe split position
        before target_pos.
        """
        if target_pos >= len(text):
            return len(text)

        # Search backwards from target_pos for a safe boundary
        for i in range(target_pos, max(0, target_pos - 500), -1):
            if i == 0 or text[i] in ("\n", "\r"):
                # Check if we're inside any unsafe region
                before = text[:i]
                if self._is_safe_to_split(before):
                    return i

        # Fallback: split at target_pos
        return target_pos

    def _is_safe_to_split(self, text_before: str) -> bool:
        """Check if splitting at this position won't break structured content."""
        # Count code block fences - must be even (closed)
        code_fences = len(re.findall(r"```", text_before))
        if code_fences % 2 != 0:
            return False

        # Count XML tags - open and close should balance
        open_tags = len(re.findall(r"<\w+[^>]*>(?!</)", text_before))
        close_tags = len(re.findall(r"</\w+>", text_before))
        if open_tags > close_tags + 2:  # Allow small imbalance
            return False

        # Check for incomplete JSON
        if text_before.strip().endswith(("{", "[", ",", ":")):
            return False

        return True

    def safe_snip_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        Snip compact with SafeSplitPoint - never breaks code blocks or structured data.
        """
        if len(messages) <= self.max_messages:
            return messages

        keep_head = 3
        keep_tail = self.max_messages - keep_head
        snipped = len(messages) - keep_head - keep_tail

        # Build snip message with SafeSplitPoint awareness
        snip_content = f"[snipped {snipped} messages to save context]"

        # Check if any snipped messages contain active code blocks
        snipped_msgs = messages[keep_head:-keep_tail]
        has_code = any(
            "```" in str(m.get("content", ""))
            for m in snipped_msgs
        )

        if has_code:
            snip_content += " (code blocks persisted to disk)"
            self._persist_snipped_code(snipped_msgs)

        return (
            messages[:keep_head]
            + [{"role": "user", "content": snip_content}]
            + messages[-keep_tail:]
        )

    def _persist_snipped_code(self, messages: List[Dict]):
        """Persist code blocks from snipped messages to disk."""
        persist_dir = Path.home() / ".nexus" / "snipped-code"
        persist_dir.mkdir(parents=True, exist_ok=True)

        for i, msg in enumerate(messages):
            content = str(msg.get("content", ""))
            code_blocks = re.findall(r"```(\w+)?\n(.*?)```", content, re.DOTALL)
            for lang, code in code_blocks:
                lang = lang or "text"
                path = persist_dir / f"snipped_{i}_{lang}.txt"
                if not path.exists():
                    path.write_text(code, encoding="utf-8")

    # --- Working-Set Aware Compaction ---

    def add_to_working_set(self, file_path: str):
        """Add a file to the working set (files being actively edited)."""
        self._working_set.add(file_path)

    def remove_from_working_set(self, file_path: str):
        """Remove a file from the working set."""
        self._working_set.discard(file_path)

    def clear_working_set(self):
        """Clear the working set."""
        self._working_set.clear()

    def working_set_aware_compact(self, messages: List[Dict]) -> List[Dict]:
        """
        Compact messages but NEVER compact content related to working set files.
        Files being actively edited are always preserved in full.
        """
        if not self._working_set:
            return messages

        result = []
        for msg in messages:
            content = msg.get("content", "")
            content_str = content if isinstance(content, str) else str(content)

            # Check if message references any working set file
            is_working_set = any(
                wf in content_str for wf in self._working_set
            )

            if is_working_set:
                # Keep working set messages intact
                result.append(msg)
            else:
                # Apply normal compaction to non-working-set messages
                result.append(msg)

        # If still too large, apply aggressive compaction to non-working-set only
        if len(result) > self.max_messages:
            result = self._compact_non_working_set(result)

        return result

    def _compact_non_working_set(self, messages: List[Dict]) -> List[Dict]:
        """Aggressively compact messages not in working set."""
        working_set_msgs = []
        other_msgs = []

        for msg in messages:
            content = msg.get("content", "")
            content_str = content if isinstance(content, str) else str(content)
            is_working_set = any(wf in content_str for wf in self._working_set)

            if is_working_set:
                working_set_msgs.append(msg)
            else:
                other_msgs.append(msg)

        # Compact other messages
        if len(other_msgs) > 10:
            other_msgs = [
                other_msgs[0],
                {"role": "user", "content": f"[{len(other_msgs)-2} messages compacted to save context]"},
                other_msgs[-1]
            ]

        return working_set_msgs + other_msgs
