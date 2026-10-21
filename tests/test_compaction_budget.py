"""Invariantes del port de budget-projection (Cline).

Cada test corresponde a un caso de
`sdk/packages/core/src/extensions/context/budget-projection/project.test.ts`
adaptado al modelo de mensajes dict de nexus-v3.
"""
from __future__ import annotations

import json

from src.core.compaction_budget import (
    build_budget_projection,
    find_latest_typed_user_message_index,
)


def estimate_chars(message: dict) -> int:
    return len(json.dumps(message, ensure_ascii=False))


def _project(messages, target, intent="agentic_summary"):
    return build_budget_projection(
        messages,
        target_tokens=target,
        policy_intent=intent,
        estimate_message_tokens=estimate_chars,
    )


def test_fails_explicitly_for_impossible_budgets():
    result = _project([{"role": "user", "content": "keep me"}], 0)
    assert result.status == "failed"
    assert len(result.messages) == 1
    assert result.warnings[0].code == "budget_impossible"
    assert result.live_tail_handling == "preserved_out_of_band"


def test_drops_unsafe_image_and_redacted_thinking_instead_of_truncating():
    result = _project([
        {"role": "assistant", "content": [
            {"type": "text", "text": "old context"},
            {"type": "redacted_thinking", "data": "x" * 500},
        ]},
        {"role": "assistant", "content": [
            {"type": "image", "data": "y" * 500, "mediaType": "image/png"},
        ]},
        {"role": "user", "content": "latest task"},
    ], 150)
    serialized = json.dumps(result.messages)
    assert "redacted_thinking" not in serialized
    assert "image/png" not in serialized
    assert any(
        action.kind == "dropped_block" and action.reason == "unsafe_to_truncate"
        for action in result.actions
    )
    assert result.live_tail_handling == "included_degraded"


def test_keeps_unsafe_blocks_when_input_is_already_under_budget():
    result = _project([{
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            {"type": "image", "data": "small-image", "mediaType": "image/png"},
        ],
    }], 1_000)
    assert result.status == "ok"
    assert result.actions == []
    assert result.live_tail_handling == "included_verbatim"
    assert "small-image" in json.dumps(result.messages)


def test_preserves_unsafe_blocks_in_latest_typed_user_message():
    result = _project([
        {"role": "user", "content": "old task " + "x" * 500},
        {"role": "user", "content": [
            {"type": "text", "text": "what is in this image?"},
            {"type": "image", "data": "live-image", "mediaType": "image/png"},
        ]},
    ], 120, intent="basic_compaction_projection")
    assert "live-image" in json.dumps(result.messages)
    assert not any(action.kind == "dropped_block" for action in result.actions)


def test_protects_latest_typed_user_after_thinking_only_messages_are_pruned():
    result = _project([
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "discard me"}]},
        {"role": "user", "content": [
            {"type": "text", "text": "what is in this image?"},
            {"type": "image", "data": "live-image", "mediaType": "image/png"},
        ]},
    ], 1_000)
    serialized = json.dumps(result.messages)
    assert "live-image" in serialized
    assert "discard me" not in serialized


def test_keeps_tool_use_and_tool_result_pairs_coherent_when_dropping_history():
    result = _project([
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool_1", "name": "read_files",
             "input": {"file_paths": ["/tmp/a.ts"]}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool_1", "name": "read_files",
             "content": "x" * 1000},
        ]},
        {"role": "user", "content": "latest task"},
    ], 140, intent="basic_compaction_projection")
    serialized = json.dumps(result.messages)
    assert "tool_1" not in serialized
    assert "latest task" in serialized
    assert any(action.reason == "tool_pair_boundary" for action in result.actions)


def test_records_budget_action_paths_against_original_message_indexes():
    result = _project([
        {"role": "assistant", "content": [
            {"type": "image", "data": "x", "mediaType": "image/png"},
        ]},
        {"role": "user", "content": "old task " + "x" * 500},
        {"role": "assistant", "content": "old answer " + "y" * 500},
        {"role": "user", "content": "latest task"},
    ], 80, intent="basic_compaction_projection")
    preserved = [
        action for action in result.actions
        if action.kind == "preserved" and action.path.message_index == 1
    ]
    dropped = [
        action for action in result.actions
        if action.kind == "dropped_message" and action.path.message_index == 2
    ]
    assert preserved
    assert dropped


def test_detects_latest_typed_user_message_when_tool_results_follow_it():
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "user", "content": "latest typed prompt"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool_1", "name": "read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool_1", "name": "read",
             "content": "result"},
        ]},
    ]
    assert find_latest_typed_user_message_index(messages) == 1


def test_preserves_the_latest_typed_prompt_under_pressure():
    result = _project([
        {"role": "user", "content": "old task " + "x" * 500},
        {"role": "user", "content": "latest typed prompt"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool_1", "name": "read",
             "content": "result " + "y" * 500},
        ]},
    ], 120)
    assert "latest typed prompt" in json.dumps(result.messages)
    assert any(action.reason == "protected_live_tail" for action in result.actions)


def test_drops_completed_tool_pairs_after_the_latest_typed_prompt():
    result = _project([
        {"role": "user", "content": "old task " + "x" * 500},
        {"role": "user", "content": "latest typed prompt"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool_after", "name": "read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool_after", "name": "read",
             "content": "huge result " + "y" * 2_000},
        ]},
    ], 140)
    serialized = json.dumps(result.messages)
    assert "latest typed prompt" in serialized
    assert "tool_after" not in serialized
    paths = {
        action.path.message_index
        for action in result.actions
        if action.kind == "dropped_message" and action.reason == "tool_pair_boundary"
    }
    assert {2, 3}.issubset(paths)


def test_preserves_unresolved_tool_use_after_the_latest_typed_prompt():
    result = _project([
        {"role": "user", "content": "old task " + "x" * 500},
        {"role": "user", "content": "latest typed prompt"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool_live", "name": "run_command",
             "input": {"command": "sleep 1"}},
        ]},
    ], 80)
    serialized = json.dumps(result.messages)
    assert "latest typed prompt" in serialized
    assert "tool_live" in serialized
    assert result.status == "failed"
    assert result.warnings[0].code == "budget_unachievable_with_protections"


def test_does_not_preserve_later_text_or_file_blocks_after_tool_result_budget_exhausted():
    result = _project([
        {"role": "user", "content": "latest typed prompt"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tool_live", "name": "read", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool_live", "name": "read",
             "content": [
                 {"type": "text", "text": "a" * 200},
                 {"type": "file", "path": "/tmp/huge.txt", "content": "b" * 1_000},
             ]},
        ]},
    ], 260)
    serialized = json.dumps(result.messages)
    assert "latest typed prompt" in serialized
    assert "b" * 100 not in serialized
    assert any(
        action.kind == "truncated_text" and action.reason == "over_budget"
        for action in result.actions
    )


def test_drops_thinking_blocks_instead_of_mutating_provider_native_reasoning():
    result = _project([
        {"role": "user", "content": "latest typed prompt"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "a" * 1_000},
            {"type": "thinking", "thinking": "b" * 1_000},
        ]},
    ], 900)
    assistant = next(m for m in result.messages if m["role"] == "assistant")
    assert "b" * 100 not in json.dumps(assistant)
    assert '"thinking"' not in json.dumps(assistant)
    assert any(
        action.kind == "dropped_block" and action.reason == "unsafe_to_truncate"
        for action in result.actions
    )


def test_drops_nested_unsafe_tool_result_blocks_outside_the_protected_tail():
    result = _project([
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tool_old", "name": "read",
             "content": [
                 {"type": "text", "text": "old output"},
                 {"type": "image", "data": "old-image-data", "mediaType": "image/png"},
             ]},
        ]},
        {"role": "user", "content": "latest typed prompt"},
    ], 1_000)
    serialized = json.dumps(result.messages)
    assert "old output" in serialized
    assert "old-image-data" not in serialized
    assert any(
        action.kind == "dropped_block" and action.reason == "unsafe_to_truncate"
        for action in result.actions
    )


def test_drops_nexus_style_images_outside_the_live_tail():
    result = _project([
        {"role": "user", "content": "old", "images": ["data:image/png;base64," + "x" * 800]},
        {"role": "user", "content": "latest typed prompt"},
    ], 100)
    serialized = json.dumps(result.messages)
    assert "base64" not in serialized
    assert "latest typed prompt" in serialized


def test_never_splits_a_nexus_tool_calls_pair_into_orphans():
    messages = [
        {"role": "user", "content": "old task " + "x" * 800},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
        ]},
        {"role": "tool", "content": "result " + "y" * 800, "tool_call_id": "call_1"},
        {"role": "user", "content": "latest typed prompt"},
    ]
    result = _project(messages, 220)
    tool_call_ids = {
        call["id"]
        for message in result.messages
        for call in (message.get("tool_calls") or [])
    }
    result_ids = {
        message.get("tool_call_id")
        for message in result.messages
        if message.get("tool_call_id")
    }
    assert tool_call_ids == result_ids, "quedo un lado del par huerfano"


def test_projection_never_drops_system_messages_by_default():
    result = _project([
        {"role": "system", "content": "SYSTEM PROMPT"},
        {"role": "user", "content": "old " + "x" * 2_000},
        {"role": "assistant", "content": "old answer " + "y" * 2_000},
        {"role": "user", "content": "latest prompt"},
    ], 120)
    assert result.messages[0]["role"] == "system"
    assert "SYSTEM PROMPT" in result.messages[0]["content"]


def test_compact_layer_keeps_system_and_latest_typed_prompt():
    import asyncio

    from src.core.compaction import CompactionPipeline

    async def run():
        pipe = CompactionPipeline({"snip": {"keep_recent": 1}})
        messages = [
            {"role": "system", "content": "instrucciones del sistema"},
            {"role": "user", "content": "TAREA ACTUAL DEL USUARIO"},
            {"role": "assistant", "content": "trabajando " + "y" * 4_000},
            {"role": "tool", "content": "resultado " + "z" * 4_000,
             "tool_call_id": "call_1"},
        ]
        out, _ = await pipe.compact(messages, limit=80)
        serialized = "\n".join(str(m.get("content") or "") for m in out)
        assert "instrucciones del sistema" in serialized
        assert "TAREA ACTUAL DEL USUARIO" in serialized

    asyncio.run(run())
