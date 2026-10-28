"""El ancla de tokens: la estimacion arranca del usage REAL del proveedor.

Caso real (2026-10-28, sesion 445b758f71fd): el request se estimo en 13.800
tokens con chars/4 y Ollama conto 16.214. Decidir poda y compactacion con el
numero chico deja el turno 17 % pasado del presupuesto, que es justo el margen
que termina en overflow y en compactaciones de emergencia.

Verificado ademas que `prompt_eval_count` de Ollama es el tamano COMPLETO del
prompt (dos requests identicas devuelven el mismo numero), asi que sirve de
ancla aunque el prefijo venga de la cache del modelo.
"""
from __future__ import annotations

import pytest

from src.core import token_anchor
from src.core.agent_loop import AgentLoop
from src.core.compaction import CompactionPipeline


@pytest.fixture(autouse=True)
def _limpiar():
    token_anchor.reset()
    yield
    token_anchor.reset()


def test_sin_ancla_devuelve_el_fallback():
    assert token_anchor.estimate("s", 1000, fallback=250) == 250
    assert token_anchor.tokens_per_char("s") == pytest.approx(0.25)
    assert token_anchor.has_anchor("s") is False


def test_con_ancla_escala_por_caracteres():
    token_anchor.record("s", chars=10_000, tokens=2_000)

    assert token_anchor.estimate("s", 10_000) == 2_000
    assert token_anchor.estimate("s", 20_000) == 4_000
    # La poda baja caracteres: la estimacion tiene que bajar en la misma medida.
    assert token_anchor.estimate("s", 5_000) == 1_000


def test_el_ancla_es_el_numero_real_no_una_heuristica():
    token_anchor.record("real", chars=33_500, tokens=16_214)

    assert token_anchor.estimate("real", 33_500) == 16_214
    assert token_anchor.tokens_per_char("real") > 0.25


def test_una_sesion_no_contamina_a_otra():
    token_anchor.record("a", chars=1_000, tokens=900)

    assert token_anchor.estimate("b", 1_000, fallback=250) == 250
    assert token_anchor.estimate("a", 1_000) == 900


def test_el_registro_no_crece_sin_limite():
    for i in range(token_anchor._MAX_SESIONES + 10):
        token_anchor.record(f"s{i}", 100, 25)

    assert len(token_anchor._ANCLA) <= token_anchor._MAX_SESIONES


def test_el_ancla_ignora_mediciones_invalidas():
    token_anchor.record("s", chars=0, tokens=100)
    token_anchor.record("s", chars=100, tokens=0)
    token_anchor.record("", chars=100, tokens=100)

    assert token_anchor.has_anchor("s") is False


def _loop() -> AgentLoop:
    loop = AgentLoop(provider_router=None, tool_registry=None, session_log=None)
    loop.compaction = CompactionPipeline(
        {"prune": {"enabled": True, "keep_recent": 3, "max_tool_result_chars": 4000}}
    )
    loop.context_max_tokens = 24576
    loop.compact_threshold = 0.75
    loop._tools_tokens_n = 7000  # type: ignore[attr-defined]
    loop._tools_chars = 28000  # type: ignore[attr-defined]
    return loop


def test_should_compact_usa_el_ancla_en_vez_de_chars_por_cuatro():
    loop = _loop()
    msgs = [{"role": "system", "content": "s" * 4000}, {"role": "user", "content": "hola"}]

    # chars/4 dice ~1k: no compacta.
    assert loop._should_compact("ancla-1", msgs, 24576) is False

    # El proveedor conto 40k para ese mismo request: ahora si compacta.
    token_anchor.record("ancla-1", loop._request_chars(msgs), 40_000)
    assert loop._should_compact("ancla-1", msgs, 24576) is True


def test_el_presupuesto_cotiza_los_schemas_a_la_densidad_medida():
    loop = _loop()
    loop._session_id = "pres-1"  # type: ignore[attr-defined]

    # Sin ancla: chars/4 sobre los schemas (7k).
    assert loop._payload_token_budget() == int(round(24576 * 0.75)) - 7000

    # Con ancla (0,484 tokens/car) los 28.000 chars de schemas son ~13.552.
    token_anchor.record("pres-1", 33_500, 16_214)
    assert loop._payload_token_budget() == int(round(24576 * 0.75)) - 13_552
    assert loop._payload_token_budget() < int(round(24576 * 0.75)) - 7000


def test_la_poda_respeta_el_presupuesto_en_tokens_reales():
    """El presupuesto y la comparacion interna tienen que hablar la misma unidad.

    Con el mismo `target_tokens`, una densidad mayor tiene que dejar menos
    caracteres: si la comparacion interna sigue en chars/4, la poda se detiene
    antes y el request real vuelve a pasarse del presupuesto.
    """
    pipe = CompactionPipeline({"prune": {"enabled": True, "keep_recent": 3, "max_tool_result_chars": 4000}})
    msgs = [{"role": "system", "content": "s" * 1000}, {"role": "user", "content": "pedido"}]
    for i in range(30):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i} " + ("x" * 60_000)})

    from src.core.compaction import _estimate_any

    con_densidad = pipe.prune_for_llm(msgs, target_tokens=20_000, tokens_per_char=0.484)
    sin_densidad = pipe.prune_for_llm(msgs, target_tokens=20_000)

    def chars(ms):
        return sum(len(str(m.get("content") or "")) for m in ms)

    # Con densidad medida la poda baja a un tope mas chico por resultado viejo.
    assert chars(con_densidad) < chars(sin_densidad)
    # Y en tokens reales el payload queda mas chico para el mismo presupuesto.
    assert _estimate_any(con_densidad, 0.484) < _estimate_any(sin_densidad, 0.484)
