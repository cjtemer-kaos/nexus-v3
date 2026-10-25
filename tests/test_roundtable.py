"""Mesa redonda: voces en paralelo, juez con JSON y degradación sin juez."""
from __future__ import annotations

import asyncio

import pytest

from src.core.roundtable import (
    DEFAULT_SEATS,
    RoundTableResult,
    SeatResult,
    _parse_judge_json,
    convene,
    lexical_overlap,
)


class FakeResp:
    def __init__(self, content: str, model: str = "fake-1"):
        self.content = content
        self.model = model
        self.usage = {"provider": "fake"}


class FakeRoute:
    def __init__(self, mid: str, provider: str = "fake", tier: int = 3):
        self.id = mid
        self.provider_name = provider
        self.tier = tier

    def effective_tier(self) -> int:
        return self.tier


class FakeRouter:
    """Router de mentira: contesta por rol, sin red y sin modelos reales."""

    def __init__(self, judge_json: str | None = None, seat_delay: float = 0.0,
                 fail_seats: tuple[str, ...] = (), empty_seats: tuple[str, ...] = ()):
        self.calls: list[dict] = []
        self.judge_json = judge_json
        self.seat_delay = seat_delay
        self.fail_seats = fail_seats
        self.empty_seats = empty_seats

    def list_models(self):
        return [FakeRoute("m-a", "fake", 2), FakeRoute("m-b", "fake", 3),
                FakeRoute("m-c", "fake", 4), FakeRoute("embed-only", "fake", 1)]

    async def chat(self, messages, tools=None, model=None, hint=None, **kw):
        system = next((m.content for m in messages if m.role == "system"), "") or ""
        user = next((m.content for m in messages if m.role == "user"), "") or ""
        self.calls.append({"system": system, "model": model, "user": user})
        if "secretario de una mesa redonda" in system:
            if self.judge_json is None:
                return FakeResp("no te doy JSON, soy un juez rebelde")
            return FakeResp(self.judge_json, model="juez")
        if any(name in system for name in self.fail_seats):
            raise RuntimeError("proveedor caído")
        if any(name in system for name in self.empty_seats):
            return FakeResp("")
        await asyncio.sleep(self.seat_delay)
        # Cada voz responde algo distinto pero con vocabulario compartido.
        return FakeResp(
            f"Desde mi silla la decision depende del costo operativo y del riesgo "
            f"de romper el despliegue. Detalle: {system[:40]}",
            model=model or "auto",
        )


def _run(coro):
    return asyncio.run(coro)


def test_parse_judge_json_tolera_fences_y_prosa():
    assert _parse_judge_json('{"a": 1}') == {"a": 1}
    assert _parse_judge_json('bla bla\n```json\n{"a": {"b": 2}}\n```\ntail') == {"a": {"b": 2}}
    assert _parse_judge_json('antes {"consensus": [], "final": "ok"} despues') == {
        "consensus": [], "final": "ok"}
    assert _parse_judge_json("sin json") is None
    assert _parse_judge_json("") is None


def test_lexical_overlap_detecta_coincidencia():
    assert lexical_overlap(["una sola voz"]) is None
    assert lexical_overlap(["", "  "]) is None
    alto = lexical_overlap([
        "conviene usar checkpoint antes de escribir archivos",
        "conviene usar checkpoint antes de escribir archivos",
    ])
    bajo = lexical_overlap([
        "conviene usar checkpoint antes de escribir archivos",
        "el riesgo esta en el despliegue y la latencia",
    ])
    assert alto == 1.0
    assert bajo is not None and bajo < alto


def test_mesa_redonda_devuelve_consenso_y_desacuerdos():
    judge = (
        '{"consensus": ["hace falta un checkpoint antes de escribir"],'
        ' "disagreements": [{"topic": "cuando correr la prueba",'
        ' "positions": ["antes del merge", "en el CI"]}],'
        ' "final": "Poné el checkpoint y dejá la prueba en el CI.", "confidence": 0.7}'
    )
    router = FakeRouter(judge_json=judge)
    res = _run(convene("¿Cómo evito romper el deploy?", router=router, seats=DEFAULT_SEATS[:3]))

    assert isinstance(res, RoundTableResult)
    assert res.answered == 3
    assert res.judge_ok is True
    assert res.judge_model == "juez"
    assert res.consensus == ["hace falta un checkpoint antes de escribir"]
    assert res.disagreements == [{"topic": "cuando correr la prueba",
                                  "positions": ["antes del merge", "en el CI"]}]
    assert res.confidence == 0.7
    assert "checkpoint" in res.final
    assert res.overlap is not None
    # 3 voces + 1 juez
    assert len(router.calls) == 4
    # Ninguna voz ve las respuestas de las otras.
    seat_users = [c["user"] for c in router.calls if "secretario" not in c["system"]]
    assert seat_users == ["¿Cómo evito romper el deploy?"] * 3


def test_mesa_redonda_corre_las_voces_en_paralelo():
    router = FakeRouter(judge_json='{"consensus": [], "final": "ok"}', seat_delay=0.25)
    loop = asyncio.new_event_loop()
    try:
        started = loop.run_until_complete(_timed(router))
    finally:
        loop.close()
    # Tres voces de 0.25 s en serie tardarían 0.75 s; en paralelo, ~0.25 s.
    assert started < 0.7


async def _timed(router):
    import time

    t0 = time.monotonic()
    await convene("tarea", router=router, seats=DEFAULT_SEATS[:3], timeout_s=10)
    return time.monotonic() - t0


def test_voz_caida_no_tumba_la_mesa():
    router = FakeRouter(judge_json='{"consensus": ["a"], "final": "f", "confidence": 0.4}',
                        fail_seats=("arquitecto-fantasma",))
    seats = [("security", "riesgos"), ("arquitecto-fantasma", "lo que sea")]
    res = _run(convene("decidir", router=router, seats=seats))

    por_silla = {s.seat: s for s in res.seats}
    assert por_silla["arquitecto-fantasma"].ok is False
    assert "RuntimeError" in por_silla["arquitecto-fantasma"].error
    assert por_silla["security"].ok is True
    assert res.answered == 1
    assert res.final == "f"


def test_respuesta_vacia_cuenta_como_caida():
    router = FakeRouter(judge_json='{"consensus": [], "final": "f"}',
                        empty_seats=("tester",))
    seats = [("tester", "pruebas"), ("code", "implementación")]
    res = _run(convene("decidir", router=router, seats=seats))
    por_silla = {s.seat: s for s in res.seats}
    assert por_silla["tester"].ok is False
    assert por_silla["tester"].error == "respuesta vacía"
    assert por_silla["code"].ok is True


def test_juez_sin_json_no_inventa_consenso():
    router = FakeRouter(judge_json=None)
    res = _run(convene("decidir", router=router, seats=DEFAULT_SEATS[:2]))
    assert res.judge_ok is False
    assert "no devolvió JSON" in res.judge_error
    assert res.consensus == []
    assert res.disagreements == []
    # Se devuelve el texto del juez, no un consenso fabricado.
    assert "rebelde" in res.final


def test_juez_sin_sintesis_se_arma_con_su_consenso():
    judge = ('{"consensus": ["archivar, no borrar"], "disagreements": '
             '[{"topic": "retención", "positions": ["180 días", "90 días"]}]}')
    router = FakeRouter(judge_json=judge)
    res = _run(convene("decidir", router=router, seats=DEFAULT_SEATS[:2]))
    assert res.judge_ok is True
    assert "archivar, no borrar" in res.final
    assert "180 días" in res.final and "90 días" in res.final
    assert "no redactó la síntesis" in res.judge_error


def test_sin_voces_no_llama_al_juez():
    router = FakeRouter(judge_json='{"consensus": [], "final": "x"}', fail_seats=("a",))
    res = _run(convene("decidir", router=router, seats=[("a", "")]))
    assert res.answered == 0
    assert res.final == ""
    assert "ninguna voz" in res.judge_error
    assert all("secretario" not in c["system"] for c in router.calls)


def test_tarea_vacia_no_gasta_llamadas():
    router = FakeRouter()
    res = _run(convene("   ", router=router))
    assert res.judge_error == "tarea vacía"
    assert router.calls == []


def test_modelos_pedidos_se_respetan_por_silla():
    router = FakeRouter(judge_json='{"consensus": [], "final": "ok"}')
    _run(convene("decidir", router=router, seats=DEFAULT_SEATS[:2],
                 models=["m-a", "m-b"]))
    usados = [c["model"] for c in router.calls if "secretario" not in c["system"]]
    assert usados == ["m-a", "m-b"]
