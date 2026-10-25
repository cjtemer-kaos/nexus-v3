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
import json
import re
import time
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path
from typing import Any

from .compaction_budget import (
    build_budget_projection,
    find_latest_typed_user_message_index,
    is_turn_start_message,
)

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


def _estimate_one(m: dict) -> int:
    """Tokens de UN mensaje. Sumar esto da el total de _estimate_messages_tokens."""
    total = 0
    c = m.get("content")
    if isinstance(c, str):
        total += estimate_tokens(c)
    elif isinstance(c, list):
        for part in c:
            if isinstance(part, dict) and "text" in part:
                total += estimate_tokens(part["text"])
            elif isinstance(part, str):
                total += estimate_tokens(part)
    if m.get("tool_calls"):
        total += len(str(m["tool_calls"])) // 4
    return total + 4


def _estimate_messages_tokens(messages: list[dict]) -> int:
    return sum(_estimate_one(m) for m in messages)


def _content_str(m: dict) -> str:
    c = m.get("content")
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


def _msg_role(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("role") or "")
    return str(getattr(m, "role", "") or "")


def _msg_content(m: Any) -> str:
    if isinstance(m, dict):
        return _content_str(m)
    c = getattr(m, "content", None)
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return str(c)


def _replace_msg_content(m: Any, content: str) -> Any:
    if isinstance(m, dict):
        return {**m, "content": content}
    try:
        return _dc_replace(m, content=content)
    except Exception:
        return m


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
        prune_cfg = self.config.get("prune", {})

        self.keep_recent: int = int(snip_cfg.get("keep_recent", 4))
        self.keep_recent_tool_results: int = int(micro_cfg.get("keep_recent", 3))
        self.max_tool_result_chars: int = int(budget_cfg.get("max_tool_result_chars", 2000))
        self.micro_placeholder: str = micro_cfg.get("placeholder", "[resultado_herramienta colapsado — ver ./data/spill/]")
        self.summary_model_hint: str = summary_cfg.get("model_hint", "simple")
        # Presupuesto dinamico del resumen (P1): min 2k, 20% del limite, techo 12k.
        self.summary_min_tokens: int = int(summary_cfg.get("min_tokens", 2000))
        self.summary_max_tokens: int = int(summary_cfg.get("max_tokens", 12000))
        self.summary_ratio: float = float(summary_cfg.get("ratio", 0.20))
        # Techo del material que se manda al summarizer (Cline presupuesta el
        # request del resumen; sin esto, un historial enorme overflowea al
        # modelo barato que resume). 0 = sin techo explicito.
        self.summary_input_max_tokens: int = int(
            summary_cfg.get("input_max_tokens", 24000))
        self.summary_min_progress: int = int(
            summary_cfg.get("minimum_progress_messages", 2))
        self.summary_failure_cooldown_s: float = float(
            summary_cfg.get("failure_cooldown_s", 600))
        self.handoff_prefix: str = str(summary_cfg.get(
            "handoff_prefix",
            "[handoff] Contexto compactado: el resumen siguiente reemplaza el "
            "historial previo. No repitas trabajo ya hecho."))
        self._summary_failed_at: float = 0.0
        self.prune_enabled: bool = bool(prune_cfg.get("enabled", True))
        self.prune_keep_recent: int = int(prune_cfg.get("keep_recent", 3))
        self.prune_max_chars: int = int(
            prune_cfg.get("max_tool_result_chars", 4000))

        persist_dir_raw = budget_cfg.get("persist_dir", "./data/spill")
        self.persist_dir: Path | None = Path(persist_dir_raw) if persist_dir_raw else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def compact(
        self, messages: list[dict], limit: int, *,
        include_summary_fallback: bool = True,
    ) -> tuple[list[dict], CompactionMetrics]:
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
            lm = LayerMetrics(name="micro", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail="placeholder collapsed")
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
        if include_summary_fallback and _estimate_messages_tokens(out) > limit:
            # Proyeccion determinista (port de Cline budget-projection): protege
            # prompts tipados, el live tail y los pares tool_use/tool_result.
            # Reemplaza el snip ciego, que podia orphanar un tool o borrar el
            # pedido actual. Si no alcanza con las protecciones, cae al fallback.
            before = _estimate_messages_tokens(out)
            projection = build_budget_projection(
                out,
                target_tokens=max(1, int(limit)),
                policy_intent="basic_compaction_projection",
                estimate_message_tokens=_estimate_one,
            )
            if projection.status == "ok":
                out = projection.messages
                detail = (
                    f"proyeccion determinista: {len(projection.actions)} acciones, "
                    f"{projection.estimated_tokens} tokens"
                )
            else:
                out = self._summary_fallback(projection.messages, limit)
                detail = "fallback agresivo; la proyeccion no alcanzo con protecciones"
            after = _estimate_messages_tokens(out)
            lm = LayerMetrics(name="summary", input_tokens=before, output_tokens=after, tokens_saved=max(0, before - after), ratio=after / before if before else 1.0, applied=True, detail=detail)
            metrics.layers.append(lm)
            metrics.layers_applied.append("summary")

        compressed = _estimate_messages_tokens(out)
        metrics.compressed_tokens = compressed
        metrics.tokens_saved = max(0, original_tokens - compressed)
        metrics.compression_ratio = compressed / original_tokens if original_tokens else 1.0
        metrics.duration_ms = (time.monotonic() - t0) * 1000
        return out, metrics

    async def summarize_with_llm(
        self,
        messages: list[dict],
        limit: int,
        router,
        model_hint: str | None = None,
        previous_summary: str = "",
        progress_messages: int = 0,
    ) -> tuple[list[dict], CompactionMetrics]:
        """L4 con LLM barato. Presupuesto dinamico, reuso y cooldown.

        No hace snip ANTES de resumir: el valor del resumen es condensar el
        "middle" que de otro modo se perderia. L1-L3 quedan como fallback.
        """
        original = list(messages)
        metrics = CompactionMetrics(
            original_tokens=_estimate_messages_tokens(original))
        if metrics.original_tokens <= int(limit):
            return original, metrics

        system_msgs = [
            m for m in original
            if _msg_role(m) == "system" and "[resumen de " not in _msg_content(m)
        ]
        rest = [m for m in original if _msg_role(m) != "system"]
        if len(rest) <= self.keep_recent + 1:
            return await self.compact(original, limit)
        middle = rest[: -self.keep_recent]
        recent = rest[-self.keep_recent:]
        middle_count = len(middle)
        # Cline nunca resume el ultimo prompt tipado: si quedo en `middle`
        # (p.ej. el loop lleva muchas tools desde que el usuario hablo), se
        # reinyecta textual para que el resumen no se lo coma.
        latest_prompt = next(
            (m for m in reversed(rest) if isinstance(m, dict) and is_turn_start_message(m)),
            None,
        )

        # Resumen previo: lo pasa el loop o se recupera del historial.
        prev = str(previous_summary or "").strip()
        if not prev:
            for _m in reversed(original):
                if _msg_role(_m) == "system" and "[resumen de " in _msg_content(_m):
                    prev = _msg_content(_m)
                    break
        # Evita anidar encabezados al reutilizar un resumen durable.
        prev = re.sub(r"^\[resumen de [^\]]*\]\s*", "", prev).strip() or prev

        # Cooldown tras un fallo del LLM de resumen.
        if (
            self._summary_failed_at
            and self.summary_failure_cooldown_s > 0
            and (time.monotonic() - self._summary_failed_at)
            < self.summary_failure_cooldown_s
        ):
            return await self.compact(original, limit)

        # Poco progreso desde el resumen anterior: reusarlo sin otra llamada.
        if prev and progress_messages < self.summary_min_progress:
            out = self._build_summary_output(
                system_msgs, prev, recent, reused=True, limit=limit,
                omitted=middle_count, latest_prompt=latest_prompt)
            if out is not None:
                self._record_summary_metric(metrics, original, out, "reuse")
                return out, metrics
            return await self.compact(original, limit)

        hint = model_hint or self.summary_model_hint
        budget = self.summary_budget(limit)
        try:
            from .provider_base import LLMMessage  # local import to avoid cycle

            routed = None
            if router is not None:
                try:
                    routed = router.resolve(task_hint=hint)
                except Exception:
                    routed = None
            if routed is None:
                return await self.compact(original, limit)

            # El material de entrada debe ser mayor que el resumen objetivo.
            middle_chars = max(12000, min(200000, budget * 8))
            # Presupuesto del request del summarizer (Cline agentic-compaction):
            # el modelo barato que resume tambien tiene ventana. Proyectamos el
            # middle a un techo y serializamos desde ahi.
            if self.summary_input_max_tokens > 0 and middle:
                input_target = max(
                    1024,
                    min(self.summary_input_max_tokens,
                        max(self.summary_min_tokens, budget) * 4),
                )
                projected = build_budget_projection(
                    middle,
                    target_tokens=input_target,
                    policy_intent="agentic_summary",
                    estimate_message_tokens=_estimate_one,
                )
                middle = projected.messages
                middle_chars = max(4096, min(middle_chars, input_target * 4))
            middle_text = "\n".join(
                f"[{_msg_role(m)}] {_msg_content(m)[:2000]}" for m in middle
            )[:middle_chars]
            prev_block = ""
            if prev:
                prev_block = (
                    "Resumen previo (reutiliza lo vigente y actualiza solo lo que cambio):\n"
                    + prev[: budget * 4]
                    + "\n\n"
                )
            llm_messages = [
                LLMMessage(role="system", content=(
                    "Sos el compactador de contexto de un agente de codigo. Resumi el historial "
                    "sin inventar nada. Usa EXACTAMENTE estas secciones:\n"
                    "1. Pedido original y objetivo\n"
                    "2. Decisiones tomadas y por que\n"
                    "3. Archivos leidos o modificados (rutas exactas)\n"
                    "4. Comandos ejecutados y resultados relevantes\n"
                    "5. Errores encontrados y como se resolvieron\n"
                    "6. Estado actual del trabajo\n"
                    "7. Pendientes concretos\n"
                    "8. Proximo paso\n"
                    "9. Datos que no se pueden perder (ids, valores, rutas)\n"
                    f"Maximo {budget} tokens. Se concreto y no repitas el historial palabra por palabra. "
                    "Responde solo con <summary>...</summary>."
                )),
                LLMMessage(role="user", content=prev_block + middle_text),
            ]
            resp = await routed.provider.chat(llm_messages, model=routed.model)
            summary_text = (resp.content or "")[: budget * 4]
            m_tag = re.search(r"<summary>(.*?)</summary>", summary_text, re.S)
            if m_tag:
                summary_text = m_tag.group(1).strip()
            summary_text = summary_text.strip()
            if not summary_text:
                raise ValueError("resumen vacio")
            out = self._build_summary_output(
                system_msgs, summary_text, recent, reused=False, limit=limit,
                omitted=middle_count, latest_prompt=latest_prompt)
            if out is None:
                return await self.compact(original, limit)
            self._summary_failed_at = 0.0
            self._record_summary_metric(metrics, original, out, f"llm={routed.model}")
            return out, metrics
        except Exception:
            # LLM summary failed — cooldown y fallback L1-L3.
            self._summary_failed_at = time.monotonic()
            return await self.compact(original, limit)

    def _build_summary_output(
        self, system_msgs: list[Any], summary_text: str, recent: list[Any], *,
        reused: bool, limit: int, omitted: int = 0,
        latest_prompt: Any | None = None,
    ) -> list[Any] | None:
        """Arma system + handoff + resumen + recent, recortando recent al limite."""
        prefix: list[Any] = list(system_msgs)
        if self.handoff_prefix:
            prefix.append({"role": "system", "content": self.handoff_prefix})
        prefix.append(self._summary_msg(omitted, summary_text, reused=reused))
        budget = max(32, int(limit) - _estimate_messages_tokens(prefix))
        kept: list[Any] = []
        for m in reversed(recent):
            c = _msg_content(m)
            cost = max(1, estimate_tokens(c))
            if cost <= budget:
                kept.append(m)
                budget -= cost
            elif budget > 16:
                cut = max(16, budget * 4 - 24)
                kept.append(_replace_msg_content(
                    m, c[:cut] + "\n[...recortado para resumen]"))
                budget = 0
                break
            else:
                break
            if budget <= 16:
                break
        kept.reverse()
        if latest_prompt is not None and not any(
            m is latest_prompt or m == latest_prompt for m in kept
        ):
            # El pedido actual tiene prioridad sobre `recent`, pero si el solo
            # no entra en el limite se recorta (con aviso) en vez de perderlo.
            available = max(
                32,
                max(1, int(limit))
                - _estimate_messages_tokens(prefix)
                - _estimate_messages_tokens(kept),
            )
            if _estimate_one(latest_prompt) > available:
                cut = max(16, available * 4 - 32)
                latest_prompt = _replace_msg_content(
                    latest_prompt,
                    _msg_content(latest_prompt)[:cut]
                    + "\n[...prompt recortado para entrar en el resumen]",
                )
            kept.insert(0, latest_prompt)
        out = prefix + kept
        # Garantia dura: el output del resumen tambien pasa por proyeccion.
        # Antes solo se rechazaba si superaba 2x el limite, asi que un resumen
        # "valido" podia disparar overflow en el request siguiente.
        projection = build_budget_projection(
            out,
            target_tokens=max(1, int(limit)),
            policy_intent="agentic_summary",
            estimate_message_tokens=_estimate_one,
        )
        if projection.status != "ok":
            return None
        return projection.messages

    def summary_budget(self, limit: int) -> int:
        """Presupuesto: max(min_tokens, limit*ratio), con techo max_tokens."""
        try:
            budget = int(int(limit) * self.summary_ratio)
        except Exception:
            budget = self.summary_min_tokens
        budget = max(self.summary_min_tokens, budget)
        budget = min(self.summary_max_tokens, budget)
        return max(128, budget)

    def _summary_msg(self, omitted: int, summary_text: str, *, reused: bool) -> dict:
        label = "reutilizado" if reused else "actualizado"
        return {
            "role": "system",
            "content": (
                f"[resumen de {omitted} mensajes anteriores — {label}]\n"
                f"{summary_text}"
            ),
        }

    def _with_summary(self, messages: list[Any], summary_text: str, *, reused: bool) -> list[Any]:
        system = [
            m for m in messages
            if _msg_role(m) == "system" and "[resumen de " not in _msg_content(m)
        ]
        rest = [m for m in messages if _msg_role(m) != "system"]
        keep = rest[-self.keep_recent:] if self.keep_recent > 0 else rest
        omitted = max(0, len(rest) - len(keep))
        out: list[Any] = list(system)
        if self.handoff_prefix:
            out.append({"role": "system", "content": self.handoff_prefix})
        out.append(self._summary_msg(omitted, summary_text, reused=reused))
        out.extend(keep)
        return out

    def _record_summary_metric(
        self, metrics: CompactionMetrics, before_msgs: list[Any],
        after_msgs: list[Any], detail: str,
    ) -> None:
        before = _estimate_messages_tokens(before_msgs)  # type: ignore[arg-type]
        after = _estimate_messages_tokens(after_msgs)  # type: ignore[arg-type]
        metrics.layers.append(LayerMetrics(
            name="summary_llm",
            input_tokens=before,
            output_tokens=after,
            tokens_saved=max(0, before - after),
            ratio=after / before if before else 1.0,
            applied=True,
            detail=detail,
        ))
        if "summary" not in metrics.layers_applied:
            metrics.layers_applied.append("summary")
        metrics.compressed_tokens = after
        metrics.tokens_saved = max(0, metrics.original_tokens - after)
        metrics.compression_ratio = after / metrics.original_tokens if metrics.original_tokens else 1.0

    def prune_for_llm(
        self,
        messages: list[Any],
        keep_recent: int | None = None,
        max_chars: int | None = None,
    ) -> list[Any]:
        """Poda cabeza+cola de resultados de tools viejos SOLO para el payload LLM.

        No muta el historial persistido. Los ultimos ``keep_recent`` resultados
        quedan intactos porque son el contexto vivo del turno.
        """
        if not self.prune_enabled:
            return list(messages)
        keep_n = self.prune_keep_recent if keep_recent is None else int(keep_recent)
        limit = self.prune_max_chars if max_chars is None else int(max_chars)
        if limit <= 0:
            return list(messages)
        tool_idx = [i for i, m in enumerate(messages) if _msg_role(m) == "tool"]
        keep = set(tool_idx[-keep_n:]) if keep_n > 0 else set()
        out: list[Any] = []
        for i, m in enumerate(messages):
            c = _msg_content(m)
            if i in keep or _msg_role(m) != "tool" or len(c) <= limit:
                out.append(m)
                continue
            head = limit // 2
            tail = limit - head
            pruned = (
                c[:head]
                + f"\n[...podado {len(c) - limit} caracteres — cola:]"
                + c[-tail:]
            )
            out.append(_replace_msg_content(m, pruned))
        return out

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
        # Nunca cortar el ultimo prompt tipado (invariante de Cline
        # `findCutIndex`): si el loop lleva muchas tools desde que el usuario
        # hablo, el snip ciego lo mandaba al medio y el resumen se lo comia.
        latest_prompt = find_latest_typed_user_message_index(rest)
        if latest_prompt >= 0 and start > latest_prompt:
            start = latest_prompt
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
                trimmed.append({**m, "content": c[: per_msg * 4] + "\n[...truncado for budget]"})
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
        # Cline exige que el request de recovery sea ESTRICTAMENTE menor antes
        # de reintentar: reintentar con el mismo tamano vuelve a fallar y quema
        # un turno. Longitud serializada es un proxy lineal suficiente.
        try:
            before = len(json.dumps(messages, ensure_ascii=False, default=str))
            after = len(json.dumps(compacted, ensure_ascii=False, default=str))
        except Exception:
            before = after = 0
        if before and after >= before:
            raise RuntimeError(
                "reactive_compact no pudo reducir el request; no se reintenta "
                "(nada recortable fuera de las protecciones)")
        return compacted

    def estimate_tokens_for_messages(self, messages: list[dict]) -> int:
        return _estimate_messages_tokens(messages)
