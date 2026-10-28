"""El piso de histeresis de compactacion se guarda en la misma base que el check.

Caso real (corrida 18, `data/perf-err.log`): `_should_compact` compara
mensajes + schemas de tools (~7k), pero el piso se guardaba solo sobre los
mensajes que quedaron tras compactar. Resultado: el check siguiente siempre
superaba el piso por los schemas y el turno volvia a compactar pagando el
resumen del LLM (~48 s con kimi-k2.7-code, medido). Ademas el log comparaba
15685 (mensajes + tools) contra 8288 (solo mensajes) e impedia ver que, con
`capas []`, no se habia compactado nada.
"""
from __future__ import annotations

from src.core.agent_loop import AgentLoop


def _loop() -> AgentLoop:
    loop = AgentLoop(provider_router=None, tool_registry=None, session_log=None)
    loop._tools_tokens_n = 7000  # type: ignore[attr-defined]
    return loop


def test_el_piso_incluye_los_schemas_de_tools():
    loop = _loop()
    metrics = {"original_tokens": 25163, "compressed_tokens": 6329, "layers": [{"name": "summary_llm"}]}

    loop._note_compaction("s1", metrics, [])

    # (6329 + 7000) * 1.2: el turno no vuelve a compactar hasta crecer de verdad.
    assert loop._no_recompact_below["s1"] == int((6329 + 7000) * 1.2)


def test_sin_original_tokens_descuenta_los_schemas_del_estimado():
    loop = _loop()
    loop._last_est_tokens = 15685  # type: ignore[attr-defined]  # mensajes + tools

    loop._note_compaction("s2", {"compressed_tokens": 8288, "layers": []}, [])

    assert loop._no_recompact_below["s2"] == int((8288 + 7000) * 1.2)


def test_el_tope_de_compactacion_descuenta_los_schemas():
    """El disparo cuenta mensajes + schemas; el pipeline mide solo mensajes."""
    loop = _loop()
    loop.compact_threshold = 0.75

    # Corrida 19: compactó "11786 -> 11786, capas []" porque el tope era 18432
    # (mensajes + tools) y los mensajes solos eran 11786.
    assert loop._limite_de_mensajes(24576) == int(round(24576 * 0.75)) - 7000
    assert loop._limite_de_mensajes(24576) < 24576 * 0.75
    # Nunca deja un tope absurdo si los schemas se comen el budget.
    assert loop._limite_de_mensajes(1000) == 1024

