"""Compaction — 4-layer pipeline + emergency reactive.

Origen: SuperNEXUS context_compactor (4 layers + metrics) + Hermes context_compressor/trajectory_compressor + Cordis compaction.
Ver BLUEPRINT.md §9.

Pipeline:
  L1 Snip    — keep system + last N, drop middle
  L2 Micro   — collapse large tool results to placeholder
  L3 Budget  — truncate + persist full content to ./data/spill/
  L4 Summary — 1 LLM call with cheap model (via provider_router hint="simple")
  Emergency  — parse context_limit from error string + aggressive compact
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@dataclass
class LayerMetrics:
    name: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_saved: int = 0
    ratio: float = 1.0
    applied: bool = False
    detail: str = ""


@dataclass
class CompactionMetrics:
    original_tokens: int = 0
    compressed_tokens: int = 0
    tokens_saved: int = 0
    compression_ratio: float = 1.0
    layers_applied: list[str] = field(default_factory=list)
    layers: list[LayerMetrics] = field(default_factory=list)
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "original_tokens": self.original_tokens,
            "compressed_tokens": self.compressed_tokens,
            "tokens_saved": self.tokens_saved,
            "compression_ratio": round(self.compression_ratio, 3),
            "layers_applied": self.layers_applied,
            "layers": [lm.__dict__ for lm in self.layers],
            "duration_ms": round(self.duration_ms, 1),
        }


def estimate_tokens(text: str) -> int:
    return len(text) // 4 if text else 0


def _estimate_messages_tokens(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        c = m.get("content")
        if c is None:
            continue
        if isinstance(c, str):
            total += estimate_tokens(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and "text" in part:
                    total += estimate_tokens(part["text"])
                elif isinstance(part, str):
                    total += estimate_tokens(part)
        # tool_calls also consume tokens
        if m.get("tool_calls"):
            total += len(str(m["tool_calls"])) // 4
    # overhead per message
    total += len(messages) * 4
    return total


def _content_str(m: dict) -> str:
    c = m.get("content")
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class CompactionPipeline:
    """Progressive: each layer only if still exceeds limit."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        snip_cfg = self.config.get("snip", {})
        micro_cfg = self.config.get("micro", {})
        budget_cfg = self.config.get("budget", {})
        summary_cfg = self.config.get("summary", {})

        self.keep_recent: int = int(snip_cfg.get("keep_recent", 4))
        self.keep_recent_tool_results: int = int(micro_cfg.get("keep_recent", 3))
        self.max_tool_result_chars: int = int(budget_cfg.get("max_tool_result_chars", 2000))
        self.micro_placeholder: str = micro_cfg.get("placeholder", "[resultado_herramienta colapsado — ver ./data/spill/]")
        self.summary_model_hint: str = summary_cfg.get("model_hint", "simple")
        self.max_summary_tokens: int = int(summary_cfg.get("max_summary_tokens", 800))

        persist_dir_raw = budget_cfg.get("persist_dir", "./data/spill")
        self.persist_dir: Path | None = Path(persist_dir_raw) if persist_dir_raw else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def compact(self, messages: list[dict], limit: int) -> tuple[list[dict], CompactionMetrics]:
        t0 = time.monotonic()
        original_tokens = _estimate_messages_tokens(messages)
        metrics = CompactionMetrics(original_tokens=original_tokens)
        out = list(messages)

        # L1 Snip
        if _estimate_messages_tokens(out) > limit:
            before = _estimate_messages_tokens(out)
            out = self._snip(out)
            after = _estimate_messages_tokens(out)
            lm = LayerMetrics(name="snip", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail=f"keep_recent={self.keep_recent}")
            metrics.layers.append(lm)
            metrics.layers_applied.append("snip")

        # L2 Micro
        if _estimate_messages_tokens(out) > limit:
            before = _estimate_messages_tokens(out)
            out = self._micro(out)
            after = _estimate_messages_tokens(out)
            lm = LayerMetrics(name="micro", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail=f"placeholder collapsed")
            metrics.layers.append(lm)
            metrics.layers_applied.append("micro")

        # L3 Budget
        if _estimate_messages_tokens(out) > limit:
            before = _estimate_messages_tokens(out)
            out = self._budget(out)
            after = _estimate_messages_tokens(out)
            lm = LayerMetrics(name="budget", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail=f"max_chars={self.max_tool_result_chars}")
            metrics.layers.append(lm)
            metrics.layers_applied.append("budget")

        # L4 Summary — requires LLM; if still over limit, mark and attempt via cheap model if router available
        if _estimate_messages_tokens(out) > limit:
            # Try to summarize middle section via cheap model if we have a router reference
            # For now, apply aggressive snip as fallback; caller can do LLM summary externally
            before = _estimate_messages_tokens(out)
            out = self._summary_fallback(out, limit)
            after = _estimate_messages_tokens(out)
            lm = LayerMetrics(name="summary", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail="fallback aggressive snip; use summarize_with_llm for LLM summary")
            metrics.layers.append(lm)
            metrics.layers_applied.append("summary")

        compressed = _estimate_messages_tokens(out)
        metrics.compressed_tokens = compressed
        metrics.tokens_saved = max(0, original_tokens - compressed)
        metrics.compression_ratio = compressed / original_tokens if original_tokens else 1.0
        metrics.duration_ms = (time.monotonic() - t0) * 1000
        return out, metrics

    async def summarize_with_llm(self, messages: list[dict], limit: int, router, model_hint: str | None = None) -> tuple[list[dict], CompactionMetrics]:
        """L4 with actual LLM call. Uses router.resolve(task_hint='simple') for cheap model.

        Falls back to compact() if router unavailable or LLM fails.
        """
        # First run L1-L3
        compacted, metrics = await self.compact(messages, limit)
        if _estimate_messages_tokens(compacted) <= limit:
            return compacted, metrics
        # Need LLM summary of middle section
        hint = model_hint or self.summary_model_hint
        try:
            from .provider_base import LLMMessage  # local import to avoid cycle

            # Build summary prompt: keep system + first + last keep_recent, summarize middle
            system_msgs = [m for m in compacted if m.get("role") == "system"]
            rest = [m for m in compacted if m.get("role") != "system"]
            if len(rest) <= self.keep_recent + 1:
                return compacted, metrics
            middle = rest[: -self.keep_recent]
            recent = rest[-self.keep_recent:]

            # Cheap summary via router
            routed = None
            if router is not None:
                try:
                    routed = router.resolve(task_hint=hint)
                except Exception:
                    routed = None

            if routed is None:
                # No LLM available — already did fallback snip
                return compacted, metrics

            # Build summarization messages: secciones fijas = el resumen no pierde
            # el estado operativo (archivos, errores, pendientes, próximo paso).
            middle_text = "\n".join(f"[{m.get('role')}] {_content_str(m)[:2000]}" for m in middle)
            llm_messages = [
                LLMMessage(role="system", content=(
                    "Sos el compactador de contexto de un agente de código. Resumí el historial "
                    "sin inventar nada. Usá EXACTAMENTE estas secciones:\n"
                    "1. Pedido original y objetivo\n"
                    "2. Decisiones tomadas y por qué\n"
                    "3. Archivos leídos o modificados (rutas exactas)\n"
                    "4. Comandos ejecutados y resultados relevantes\n"
                    "5. Errores encontrados y cómo se resolvieron\n"
                    "6. Estado actual del trabajo\n"
                    "7. Pendientes concretos\n"
                    "8. Próximo paso\n"
                    "9. Datos que no se pueden perder (ids, valores, rutas)\n"
                    f"Máximo {self.max_summary_tokens} tokens. Respondé solo con <summary>...</summary>."
                )),
                LLMMessage(role="user", content=middle_text[:12000]),
            ]
            resp = await routed.provider.chat(llm_messages, model=routed.model)
            summary_text = (resp.content or "")[: self.max_summary_tokens * 4]
            m_tag = re.search(r"<summary>(.*?)</summary>", summary_text, re.S)
            if m_tag:
                summary_text = m_tag.group(1).strip()
            summary_msg = {"role": "system", "content": f"[resumen de {len(middle)} mensajes anteriores]\n{summary_text}"}
            out = system_msgs + [summary_msg] + recent
            # Recompute metrics
            before = _estimate_messages_tokens(compacted)
            after = _estimate_messages_tokens(out)
            lm = LayerMetrics(name="summary_llm", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail=f"llm={routed.model}")
            metrics.layers.append(lm)
            if "summary" not in metrics.layers_applied:
                metrics.layers_applied.append("summary")
            metrics.compressed_tokens = after
            metrics.tokens_saved = max(0, metrics.original_tokens - after)
            metrics.compression_ratio = after / metrics.original_tokens if metrics.original_tokens else 1.0
            return out, metrics
        except Exception as e:
            # LLM summary failed — return previous compacted
            return compacted, metrics

    def maybe_compact(self, messages: list[dict], max_tokens: int, threshold: float = 0.75) -> bool:
        """Return True if compaction should be triggered (tokens > threshold*max_tokens)."""
        tokens = _estimate_messages_tokens(messages)
        return tokens > int(max_tokens * threshold)

    # ------------------------------------------------------------------
    # Layer implementations
    # ------------------------------------------------------------------
    def _snip(self, messages: list[dict]) -> list[dict]:
        if len(messages) <= self.keep_recent + 1:
            return messages
        system = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        if len(rest) <= self.keep_recent:
            return messages
        start = len(rest) - self.keep_recent
        # Nunca dejar un `tool` huérfano: si el primer mensaje retenido es un
        # resultado, retrocedemos hasta su `assistant(tool_calls)` (si no, el
        # proveedor rechaza el historial o el resultado queda sin contexto).
        while start > 0 and rest[start].get("role") == "tool":
            start -= 1
        keep = rest[start:]
        omitted = len(rest) - len(keep)
        marker = {"role": "system", "content": f"[recorte: {omitted} mensajes omitidos — compactado en {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}]"}
        return system + [marker] + keep

    def _micro(self, messages: list[dict]) -> list[dict]:
        # Los últimos N resultados de tools son el contexto vivo del turno:
        # colapsarlos mata lo que el modelo acaba de pedir.
        tool_idx = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        keep = set(tool_idx[-self.keep_recent_tool_results:]) if self.keep_recent_tool_results > 0 else set()
        out: list[dict] = []
        for i, m in enumerate(messages):
            c = _content_str(m)
            if i in keep:
                out.append(m)
                continue
            if m.get("role") == "tool" and len(c) > self.max_tool_result_chars:
                # Persist full if dir configured
                if self.persist_dir is not None:
                    self._persist_content(c, m)
                out.append({**m, "content": self.micro_placeholder})
            else:
                out.append(m)
        return out

    def _budget(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            c = _content_str(m)
            if c and len(c) > self.max_tool_result_chars and m.get("role") != "system":
                preview = c[: self.max_tool_result_chars]
                remaining = len(c) - len(preview)
                if self.persist_dir is not None:
                    self._persist_content(c, m)
                    suffix = f"\n[...truncado {remaining} caracteres — completo persistido en {self.persist_dir}/ — vista previa arriba]"
                else:
                    suffix = f"\n[...truncado {remaining} caracteres]"
                out.append({**m, "content": preview + suffix})
            else:
                out.append(m)
        return out

    def _summary_fallback(self, messages: list[dict], limit: int) -> list[dict]:
        """Aggressive fallback when LLM summary unavailable: keep system + last keep_recent."""
        system = [m for m in messages if m.get("role") == "system"]
        rest = [m for m in messages if m.get("role") != "system"]
        if len(rest) <= self.keep_recent:
            return messages
        # Also truncate each remaining message to fit limit
        keep = rest[-self.keep_recent :]
        # Estimate per-msg budget
        system_tokens = _estimate_messages_tokens(system)
        remaining = max(200, limit - system_tokens - 100)
        per_msg = max(200, remaining // max(1, len(keep)))
        trimmed: list[dict] = []
        for m in keep:
            c = _content_str(m)
            if estimate_tokens(c) > per_msg:
                trimmed.append({**m, "content": c[: per_msg * 4] + f"\n[...truncado for budget]"})
            else:
                trimmed.append(m)
        marker = {"role": "system", "content": f"[respaldo resumen emergencia: {len(rest)-len(keep)} mensajes descartados]"}
        return system + [marker] + trimmed

    def _persist_content(self, content: str, msg: dict) -> Path | None:
        if self.persist_dir is None:
            return None
        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            h = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = f"spill_{ts}_{h}.txt"
            p = self.persist_dir / fname
            p.write_text(content, encoding="utf-8")
            return p
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Emergency reactive (BLUEPRINT §9.2)
    # ------------------------------------------------------------------
    async def reactive_compact(self, messages: list[dict], error: str) -> list[dict]:
        """Emergency: parse context_limit from error and compact aggressively.

        Handles patterns:
          - context_limit 32000, got 45000
          - prompt_too_long, max 8192
          - context window exceeded ... limit 4096
        """
        # Try to extract limit
        limit: int | None = None
        patterns = [
            r"context[_\s]?limit\D*?(\d{3,6})",
            r"max[_\s]?tokens\D*?(\d{3,6})",
            r"context window\D*?(\d{3,6})",
            r"too long\D*?(\d{3,6})",
            r"maximum.*?(\d{3,6})",
        ]
        for pat in patterns:
            m = re.search(pat, error, re.I)
            if m:
                try:
                    limit = int(m.group(1))
                    break
                except Exception:
                    continue
        target = int(limit * 0.60) if limit else 8000
        compacted, _ = await self.compact(messages, target)
        return compacted

    def estimate_tokens_for_messages(self, messages: list[dict]) -> int:
        return _estimate_messages_tokens(messages)
