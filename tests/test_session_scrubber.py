"""Tests del SequenceScrubber: alternancia de turnos y no-mutacion del caller."""
from src.core.session_manager import SequenceScrubber


def _roles(msgs):
    return [m["role"] for m in msgs]


def test_empty_returns_empty():
    assert SequenceScrubber.scrub([]) == []


def test_merges_consecutive_assistant_messages():
    out = SequenceScrubber.scrub([
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "uno"},
        {"role": "assistant", "content": "dos"},
        {"role": "user", "content": "seguimos"},
    ])
    assert _roles(out) == ["user", "assistant", "user"]
    assert "uno" in out[1]["content"] and "dos" in out[1]["content"]


def test_groups_tool_results_into_user_block():
    out = SequenceScrubber.scrub([
        {"role": "user", "content": "leé el archivo"},
        {"role": "tool", "content": "contenido A"},
        {"role": "tool", "content": "contenido B"},
        {"role": "assistant", "content": "listo"},
        {"role": "user", "content": "gracias"},
    ])
    assert _roles(out) == ["user", "assistant", "user"]
    # los dos resultados quedan agrupados en el primer bloque de usuario
    assert "[Tool Results]" in out[0]["content"]
    assert "contenido A" in out[0]["content"] and "contenido B" in out[0]["content"]


def test_no_adjacent_same_role_remains():
    out = SequenceScrubber.scrub([
        {"role": "system", "content": "sos un agente"},
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
    ])
    roles = _roles(out)
    for a, b in zip(roles, roles[1:]):
        assert a != b, roles


def test_drops_trailing_assistant_so_model_can_answer():
    out = SequenceScrubber.scrub([
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "respuesta vieja"},
    ])
    assert out[-1]["role"] == "user"


def test_does_not_mutate_caller_dicts():
    msgs = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "uno"},
        {"role": "assistant", "content": "dos"},
    ]
    original = [dict(m) for m in msgs]
    SequenceScrubber.scrub(msgs)
    assert msgs == original, "scrub mutó los dicts del llamador"
