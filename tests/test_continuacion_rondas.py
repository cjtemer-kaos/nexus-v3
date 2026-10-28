"""Continuacion por rondas: cuando el harness debe seguir trabajando solo."""

from __future__ import annotations

from src.core.agent_loop import LoopStep, turno_hizo_trabajo


class _Res:
    def __init__(self, final_output: str, pasos: list[tuple[str, str]]):
        self.final_output = final_output
        self.steps = [LoopStep(iteration=1, phase=p, content="", tool_used=t) for p, t in pasos]


def test_sigue_si_uso_herramientas_y_no_cerro():
    assert turno_hizo_trabajo(_Res("## Que hice\nfalta el punto 1", [("act", "exec")])) is True


def test_no_sigue_si_no_uso_herramientas():
    assert turno_hizo_trabajo(_Res("listo, respondo", [("decide", "")])) is False


def test_no_sigue_si_marco_listo():
    assert turno_hizo_trabajo(_Res("LISTO: el cliente descarga", [("act", "exec")])) is False


def test_sin_steps_no_sigue():
    assert turno_hizo_trabajo(object()) is False
