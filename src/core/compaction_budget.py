"""Proyeccion determinista de presupuesto de contexto.

Port de `sdk/packages/core/src/extensions/context/budget-projection/project.ts`
de Cline (commit e9dafbb, Apache-2.0), adaptado al modelo de mensajes dict de
nexus-v3 (`role`, `content`, `tool_calls`, `tool_call_id`, `images`).

Que resuelve:
- Antes de mandar un request (o de resumir), garantiza que el transcript entre
  en un target de tokens SIN romper pares `tool_call`/`tool_result`.
- Protege el primer prompt tipado y el ultimo prompt tipado.
- Protege el "live tail": el ultimo `tool_calls` sin resolver y todo lo que le
  sigue, porque el modelo esta esperando ese resultado.
- Elimina thinking e imagenes fuera del tail protegido (no se pueden truncar:
  mutilar un bloque nativo o un base64 lo vuelve invalido).
- Trunca texto desde el final, y recien despues descarta mensajes desde el
  principio, cerrando cada descarte sobre el par de tool completo.
- Devuelve `actions` auditables y warnings explicitos cuando el target es
  imposible o inalcanzable con las protecciones.

Invariantes (testeadas):
1. Nunca se corta un par tool_use/tool_result: si se descarta un lado, se
   descarta el otro (closure transitivo por id).
2. El ultimo prompt tipado jamas se descarta ni se trunca.
3. Un `tool_call` sin resultado y su cola quedan intactos.
4. Los indices de `actions.path.message_index` son los del transcript ORIGINAL,
   aunque el mensaje se haya movido o eliminado.
5. `status == "failed"` implica un warning con codigo estable; nunca se
   devuelve un transcript vacio silenciosamente.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

Message = dict[str, Any]
EstimateFn = Callable[[Message], int]

PolicyIntent = Literal[
    "agentic_summary", "basic_compaction_projection", "normal_provider_request"
]
ActionKind = Literal["truncated_text", "dropped_block", "dropped_message", "preserved"]
ActionReason = Literal[
    "over_budget", "unsafe_to_truncate", "tool_pair_boundary", "protected_live_tail"
]
LiveTailHandling = Literal[
    "included_verbatim",
    "included_degraded",
    "summarized_as_context",
    "omitted_with_warning",
    "preserved_out_of_band",
]

SYNTHETIC_KINDS = frozenset({
    "auto_compaction",
    "compaction_budget_emergency",
    "completion_reminder",
    "loop_detection_notice",
    "manual_compaction",
    "mistake_stop_notice",
    "recovery_notice",
    "compaction_summary",
})

SYNTHETIC_USER_PREFIXES = (
    "[resumen de ",
    "[handoff]",
    "[steer del usuario]",
    "[recorte:",
)

_UNSAFE_BLOCK_TYPES = frozenset({"image", "media", "redacted_thinking"})
_THINKING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


@dataclass(frozen=True, slots=True)
class BudgetPath:
    message_index: int
    block_index: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetAction:
    kind: ActionKind
    path: BudgetPath
    reason: ActionReason
    original_size: int
    final_size: int


@dataclass(frozen=True, slots=True)
class BudgetWarning:
    code: str
    message: str


@dataclass(slots=True)
class BudgetProjection:
    status: Literal["ok", "failed"]
    messages: list[Message]
    actions: list[BudgetAction] = field(default_factory=list)
    live_tail_handling: LiveTailHandling = "included_verbatim"
    estimated_tokens: int = 0
    warnings: list[BudgetWarning] = field(default_factory=list)

    @property
    def action_count(self) -> int:
        return len(self.actions)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)


@dataclass(frozen=True, slots=True)
class _Policy:
    protect_latest_typed_user: bool
    protect_live_tail_from_drop: bool
    drop_unsafe_outside_live_tail: bool
    drop_thinking_blocks: bool


def resolve_projection_policy(intent: str) -> _Policy:
    if intent in ("agentic_summary", "basic_compaction_projection"):
        return _Policy(True, True, True, True)
    if intent == "normal_provider_request":
        return _Policy(True, True, False, False)
    raise ValueError(f"policy_intent desconocido: {intent!r}")


# ---------------------------------------------------------------------------
# Modelo de mensajes (adaptado a dicts OpenAI-like)
# ---------------------------------------------------------------------------

def _content_blocks(message: Message) -> list[Any] | None:
    content = message.get("content")
    return content if isinstance(content, list) else None


def _metadata(message: Message) -> dict[str, Any]:
    metadata = message.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                for key in ("text", "thinking", "content"):
                    value = block.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(parts)
    return ""


def is_compaction_summary_message(message: Message) -> bool:
    metadata = _metadata(message)
    if metadata.get("kind") == "compaction_summary":
        return True
    text = _text_of(message.get("content")).lstrip()
    return text.startswith("[resumen de ") or text.startswith("[handoff]")


def is_tool_result_only_user_message(message: Message) -> bool:
    if message.get("role") != "user":
        return False
    blocks = _content_blocks(message)
    if not blocks:
        return False
    return all(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in blocks
    )


def is_synthetic_message(message: Message) -> bool:
    metadata = _metadata(message)
    if metadata.get("synthetic") is True:
        return True
    if metadata.get("kind") in SYNTHETIC_KINDS:
        return True
    if message.get("role") != "user":
        return False
    text = _text_of(message.get("content")).lstrip()
    return any(text.startswith(prefix) for prefix in SYNTHETIC_USER_PREFIXES)


def is_turn_start_message(message: Message) -> bool:
    """Un turno tipado: prompt real del usuario, no resultado ni sintetico."""
    if message.get("role") != "user":
        return False
    if _metadata(message).get("userRunSpan") == 0:
        return False
    if is_tool_result_only_user_message(message):
        return False
    if is_compaction_summary_message(message):
        return False
    if is_synthetic_message(message):
        return False
    return bool(_text_of(message.get("content")).strip())


def find_first_typed_user_message_index(messages: list[Message]) -> int:
    for index, message in enumerate(messages):
        if is_turn_start_message(message):
            return index
    return -1


def find_latest_typed_user_message_index(messages: list[Message]) -> int:
    for index in range(len(messages) - 1, -1, -1):
        if is_turn_start_message(messages[index]):
            return index
    return -1


def collect_tool_ids(message: Message) -> set[str]:
    ids: set[str] = set()
    tool_call_id = message.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id:
        ids.add(tool_call_id)
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                ids.add(call_id)
    for block in _content_blocks(message) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and isinstance(block.get("id"), str):
            ids.add(block["id"])
        elif block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
            ids.add(block["tool_use_id"])
    return ids


def build_tool_pair_index(messages: list[Message]) -> dict[str, set[int]]:
    index: dict[str, set[int]] = {}
    for message_index, message in enumerate(messages):
        for tool_id in collect_tool_ids(message):
            index.setdefault(tool_id, set()).add(message_index)
    return index


def find_protected_tail_start_index(messages: list[Message]) -> int:
    resolved: set[str] = set()
    for message in messages:
        if message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str):
            resolved.add(message["tool_call_id"])
        for block in _content_blocks(message) or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    resolved.add(tool_use_id)
    for index in range(len(messages) - 1, -1, -1):
        calls = messages[index].get("tool_calls") or []
        if any(
            isinstance(call, dict) and call.get("id") not in resolved
            for call in calls
        ):
            return index
        for block in _content_blocks(messages[index]) or []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") not in resolved
            ):
                return index
    return len(messages)


def collect_message_closure(messages: list[Message], start_index: int) -> set[int]:
    pair_index = build_tool_pair_index(messages)
    removal: set[int] = set()
    queue = [start_index]
    while queue:
        index = queue.pop(0)
        if index in removal or not (0 <= index < len(messages)):
            continue
        removal.add(index)
        for tool_id in collect_tool_ids(messages[index]):
            for linked in pair_index.get(tool_id, set()):
                if linked not in removal:
                    queue.append(linked)
    return removal


# ---------------------------------------------------------------------------
# Tamaños, clonado y truncado
# ---------------------------------------------------------------------------

def safe_json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def clone_messages(messages: Iterable[Message]) -> list[Message]:
    return copy.deepcopy(list(messages))


def default_estimate_message_tokens(message: Message) -> int:
    text = _text_of(message.get("content"))
    images = message.get("images") or []
    tool_calls = message.get("tool_calls") or []
    extra = safe_json_size(tool_calls) if tool_calls else 0
    return max(1, len(text) // 4 + extra // 4 + len(images) * 800 + 4)


def total_tokens(messages: list[Message], estimate: EstimateFn) -> int:
    return sum(estimate(message) for message in messages)


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 16:
        return text[: max(1, max_chars)]
    marker_estimate = f"\n...[truncated {len(text) - max_chars} chars]"
    keep = max(1, max_chars - len(marker_estimate))
    marker = f"\n...[truncated {len(text) - keep} chars]"
    return f"{text[:keep]}{marker}"


def _truncate_tool_result_content(content: Any, max_chars: int) -> Any:
    if isinstance(content, str):
        return truncate_text(content, max_chars)
    if not isinstance(content, list):
        return content
    remaining = max_chars
    out: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        if remaining <= 0:
            if block.get("type") == "text":
                out.append({**block, "text": ""})
            elif block.get("type") == "file":
                out.append({**block, "content": ""})
            else:
                out.append(block)
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            text = truncate_text(block["text"], remaining)
            remaining -= len(text)
            out.append({**block, "text": text})
        elif block.get("type") == "file" and isinstance(block.get("content"), str):
            body = truncate_text(block["content"], remaining)
            remaining -= len(body)
            out.append({**block, "content": body})
        else:
            out.append(block)
    return out


def _tool_result_text_length(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            total += len(block["text"])
        elif block.get("type") == "file" and isinstance(block.get("content"), str):
            total += len(block["content"])
    return total


def truncate_message_text(message: Message, max_chars: int) -> Message:
    content = message.get("content")
    if isinstance(content, str):
        return {**message, "content": truncate_text(content, max_chars)}
    if not isinstance(content, list):
        return message

    remaining = max_chars
    out: list[Any] = []
    for block in content:
        if not isinstance(block, dict):
            out.append(block)
            continue
        block_type = block.get("type")
        if remaining <= 0:
            if block_type == "text":
                out.append({**block, "text": ""})
            elif block_type == "file":
                out.append({**block, "content": ""})
            elif block_type == "tool_result":
                out.append({**block, "content": _truncate_tool_result_content(block.get("content"), 0)})
            else:
                out.append(block)
            continue
        if block_type == "text" and isinstance(block.get("text"), str):
            text = truncate_text(block["text"], remaining)
            remaining -= len(text)
            out.append({**block, "text": text})
        elif block_type == "file" and isinstance(block.get("content"), str):
            body = truncate_text(block["content"], remaining)
            remaining -= len(body)
            out.append({**block, "content": body})
        elif block_type == "tool_result":
            body = _truncate_tool_result_content(block.get("content"), remaining)
            remaining -= _tool_result_text_length(body)
            out.append({**block, "content": body})
        else:
            out.append(block)
    return {**message, "content": out}


def has_truncatable_text(message: Message) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return bool(content)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict)
        and block.get("type") in ("text", "file", "tool_result")
        for block in content
    )


# ---------------------------------------------------------------------------
# Poda de bloques no truncables
# ---------------------------------------------------------------------------

def _prune_empty_messages(
    messages: list[Message],
    original_indexes: list[int],
    actions: list[BudgetAction],
    reason: ActionReason = "over_budget",
) -> tuple[list[Message], list[int]]:
    next_messages: list[Message] = []
    next_indexes: list[int] = []
    for index, message in enumerate(messages):
        content = message.get("content")
        is_empty = content in (None, "", [])
        has_side_data = bool(message.get("tool_calls") or message.get("images"))
        if is_empty and not has_side_data:
            actions.append(BudgetAction(
                kind="dropped_message",
                path=BudgetPath(original_indexes[index]),
                reason=reason,
                original_size=safe_json_size(message),
                final_size=0,
            ))
            continue
        next_messages.append(message)
        next_indexes.append(original_indexes[index])
    return next_messages, next_indexes


def _drop_thinking_blocks(
    messages: list[Message],
    original_indexes: list[int],
    actions: list[BudgetAction],
) -> list[Message]:
    out: list[Message] = []
    for message_index, message in enumerate(messages):
        changed = False
        original_size = safe_json_size(message)
        next_message = dict(message)

        if "reasoning_content" in next_message and next_message.get("reasoning_content"):
            next_message.pop("reasoning_content", None)
            changed = True
            actions.append(BudgetAction(
                kind="dropped_block",
                path=BudgetPath(original_indexes[message_index]),
                reason="unsafe_to_truncate",
                original_size=original_size,
                final_size=safe_json_size(next_message),
            ))

        blocks = _content_blocks(next_message)
        if blocks is not None:
            kept: list[Any] = []
            for block_index, block in enumerate(blocks):
                if isinstance(block, dict) and block.get("type") in _THINKING_BLOCK_TYPES:
                    changed = True
                    actions.append(BudgetAction(
                        kind="dropped_block",
                        path=BudgetPath(original_indexes[message_index], block_index),
                        reason="unsafe_to_truncate",
                        original_size=safe_json_size(block),
                        final_size=0,
                    ))
                    continue
                kept.append(block)
            if changed:
                next_message["content"] = kept

        out.append(next_message if changed else message)
    return out


def _drop_unsafe_blocks(
    messages: list[Message],
    original_indexes: list[int],
    actions: list[BudgetAction],
    latest_typed_user_index: int,
    protected_tail_start_index: int,
) -> list[Message]:
    out: list[Message] = []
    for message_index, message in enumerate(messages):
        protected = (
            message_index == latest_typed_user_index
            or message_index >= protected_tail_start_index
        )
        if protected:
            out.append(message)
            continue

        changed = False
        next_message = dict(message)
        original_size = safe_json_size(message)

        images = next_message.get("images")
        if images:
            next_message.pop("images", None)
            changed = True
            actions.append(BudgetAction(
                kind="dropped_block",
                path=BudgetPath(original_indexes[message_index]),
                reason="unsafe_to_truncate",
                original_size=safe_json_size(images),
                final_size=0,
            ))

        blocks = _content_blocks(next_message)
        if blocks is not None:
            kept: list[Any] = []
            for block_index, block in enumerate(blocks):
                if isinstance(block, dict) and block.get("type") in _UNSAFE_BLOCK_TYPES:
                    changed = True
                    actions.append(BudgetAction(
                        kind="dropped_block",
                        path=BudgetPath(original_indexes[message_index], block_index),
                        reason="unsafe_to_truncate",
                        original_size=safe_json_size(block),
                        final_size=0,
                    ))
                    continue
                if isinstance(block, dict) and block.get("type") == "tool_result" and isinstance(block.get("content"), list):
                    nested = [
                        nested_block
                        for nested_block in block["content"]
                        if not (
                            isinstance(nested_block, dict)
                            and nested_block.get("type") in _UNSAFE_BLOCK_TYPES
                        )
                    ]
                    if len(nested) != len(block["content"]):
                        changed = True
                        next_block = {**block, "content": nested}
                        actions.append(BudgetAction(
                            kind="dropped_block",
                            path=BudgetPath(original_indexes[message_index], block_index),
                            reason="unsafe_to_truncate",
                            original_size=safe_json_size(block),
                            final_size=safe_json_size(next_block),
                        ))
                        kept.append(next_block)
                        continue
                kept.append(block)
            if changed:
                next_message["content"] = kept

        if changed:
            actions.append(BudgetAction(
                kind="dropped_block",
                path=BudgetPath(original_indexes[message_index]),
                reason="unsafe_to_truncate",
                original_size=original_size,
                final_size=safe_json_size(next_message),
            ))
        out.append(next_message if changed else message)
    return out


# ---------------------------------------------------------------------------
# Proyeccion
# ---------------------------------------------------------------------------

def build_budget_projection(
    messages: list[Message],
    target_tokens: int,
    policy_intent: str = "agentic_summary",
    estimate_message_tokens: EstimateFn | None = None,
    preserve_roles: Iterable[str] = ("system",),
) -> BudgetProjection:
    estimate = estimate_message_tokens or default_estimate_message_tokens
    policy = resolve_projection_policy(policy_intent)
    pinned_roles = frozenset(preserve_roles)
    actions: list[BudgetAction] = []
    warnings: list[BudgetWarning] = []

    if target_tokens <= 0:
        return BudgetProjection(
            status="failed",
            messages=clone_messages(messages),
            actions=actions,
            live_tail_handling="preserved_out_of_band",
            estimated_tokens=total_tokens(messages, estimate),
            warnings=[BudgetWarning(
                code="budget_impossible",
                message="Target budget must be greater than zero.",
            )],
        )

    working = clone_messages(messages)
    original_indexes = list(range(len(working)))

    if policy.drop_thinking_blocks:
        working, original_indexes = _prune_empty_messages(
            _drop_thinking_blocks(working, original_indexes, actions),
            original_indexes,
            actions,
            "unsafe_to_truncate",
        )

    latest_typed_user_index = (
        find_latest_typed_user_message_index(working)
        if policy.protect_latest_typed_user
        else -1
    )
    protected_tail_start_index = (
        find_protected_tail_start_index(working)
        if policy.protect_live_tail_from_drop
        else len(working)
    )

    if policy.drop_unsafe_outside_live_tail:
        working, original_indexes = _prune_empty_messages(
            _drop_unsafe_blocks(
                working,
                original_indexes,
                actions,
                latest_typed_user_index,
                protected_tail_start_index,
            ),
            original_indexes,
            actions,
        )

    estimated_tokens = total_tokens(working, estimate)
    if estimated_tokens <= target_tokens:
        return BudgetProjection(
            status="ok",
            messages=working,
            actions=actions,
            live_tail_handling="included_degraded" if actions else "included_verbatim",
            estimated_tokens=estimated_tokens,
            warnings=warnings,
        )

    # 1) Truncar texto desde el final. Nunca el ultimo prompt tipado ni el tail.
    for index in range(len(working) - 1, -1, -1):
        if estimated_tokens <= target_tokens:
            break
        latest = find_latest_typed_user_message_index(working)
        if index == latest:
            continue
        if (
            policy.protect_live_tail_from_drop
            and index >= find_protected_tail_start_index(working)
        ):
            continue
        if not has_truncatable_text(working[index]):
            continue
        original_size = safe_json_size(working[index])
        chars_per_token = max(
            1,
            original_size / max(1, estimate(working[index])),
        )
        target_chars = max(
            16,
            int(target_tokens * chars_per_token / max(1, len(working))),
        )
        working[index] = truncate_message_text(working[index], target_chars)
        actions.append(BudgetAction(
            kind="truncated_text",
            path=BudgetPath(original_indexes[index]),
            reason="over_budget",
            original_size=original_size,
            final_size=safe_json_size(working[index]),
        ))
        estimated_tokens = total_tokens(working, estimate)

    # 2) Descartar desde el principio, cerrando cada par de tools.
    preserved_seen: set[int] = set()
    index = 0
    while index < len(working) and estimated_tokens > target_tokens:
        first_typed = find_first_typed_user_message_index(working)
        latest_typed = find_latest_typed_user_message_index(working)
        protected_start = (
            find_protected_tail_start_index(working)
            if policy.protect_live_tail_from_drop
            else len(working)
        )
        if index in (first_typed, latest_typed):
            original_index = original_indexes[index]
            if original_index not in preserved_seen:
                preserved_seen.add(original_index)
                actions.append(BudgetAction(
                    kind="preserved",
                    path=BudgetPath(original_index),
                    reason="protected_live_tail",
                    original_size=safe_json_size(working[index]),
                    final_size=safe_json_size(working[index]),
                ))
            index += 1
            continue

        if working[index].get("role") in pinned_roles:
            original_index = original_indexes[index]
            if original_index not in preserved_seen:
                preserved_seen.add(original_index)
                actions.append(BudgetAction(
                    kind="preserved",
                    path=BudgetPath(original_index),
                    reason="protected_live_tail",
                    original_size=safe_json_size(working[index]),
                    final_size=safe_json_size(working[index]),
                ))
            index += 1
            continue

        closure = collect_message_closure(working, index)
        if first_typed in closure or latest_typed in closure:
            index += 1
            continue
        if any(
            working[removal_index].get("role") in pinned_roles
            for removal_index in closure
        ):
            index += 1
            continue
        if any(removal_index >= protected_start for removal_index in closure):
            index += 1
            continue

        for removal_index in sorted(closure):
            needs_pair_reason = (
                len(closure) > 1 or bool(collect_tool_ids(working[removal_index]))
            )
            actions.append(BudgetAction(
                kind="dropped_message",
                path=BudgetPath(original_indexes[removal_index]),
                reason="tool_pair_boundary" if needs_pair_reason else "over_budget",
                original_size=safe_json_size(working[removal_index]),
                final_size=0,
            ))
        working = [
            message for position, message in enumerate(working)
            if position not in closure
        ]
        original_indexes = [
            original for position, original in enumerate(original_indexes)
            if position not in closure
        ]
        estimated_tokens = total_tokens(working, estimate)

    if estimated_tokens > target_tokens:
        warnings.append(BudgetWarning(
            code="budget_unachievable_with_protections",
            message="Projection could not reach budget without violating protected content.",
        ))
        return BudgetProjection(
            status="failed",
            messages=working,
            actions=actions,
            live_tail_handling="included_degraded",
            estimated_tokens=estimated_tokens,
            warnings=warnings,
        )

    return BudgetProjection(
        status="ok",
        messages=working,
        actions=actions,
        live_tail_handling="included_degraded" if actions else "included_verbatim",
        estimated_tokens=estimated_tokens,
        warnings=warnings,
    )
