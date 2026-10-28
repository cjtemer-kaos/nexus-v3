"""Reintento por saturacion del upstream (capacidad), no por request invalido.

Caso real (opencode-go, 2026-10-28):
  Upstream request failed: [ttft_admission_refused] the cell is at capacity;
  retry shortly (after 49 s)

El arnes lo tomaba como "[Error LLM: ...]" y contaba la ronda como caida. Con un
pedido de horas eso corta el trabajo por algo que se resuelve esperando lo que el
propio servidor pide.
"""

from __future__ import annotations

import asyncio

import pytest

from src.providers import cloud_provider as cp


class _Resp:
    def __init__(self, status: int, body: str = "", data: dict | None = None):
        self.status_code = status
        self.text = body
        self._data = data or {}

    def json(self):
        return self._data


class _Cliente:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas = 0

    async def post(self, url=None, json=None, headers=None):
        self.llamadas += 1
        r = self.respuestas.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class _Aio:
    """Shim: registra las esperas sin dormir."""

    def __init__(self):
        self.dormido: list[float] = []

    async def sleep(self, s):
        self.dormido.append(s)


def _proveedor(monkeypatch, respuestas):
    p = cp.CloudProvider(base_url="http://x", api_key="k")
    p._client = _Cliente(respuestas)  # type: ignore[assignment]
    aio = _Aio()
    monkeypatch.setattr(cp, "asyncio", aio)
    return p, aio


def _ok(content: str = "listo") -> _Resp:
    return _Resp(200, data={"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


def _chat(p):
    return asyncio.run(p.chat([cp.LLMMessage(role="user", content="hola")], model="m"))


def test_espera_sugerida_lee_el_texto_del_upstream():
    assert cp.espera_sugerida("the cell is at capacity; retry shortly (after 49 s)") == 49.0
    assert cp.espera_sugerida("retry in 5s") == 5.0
    assert cp.espera_sugerida("sin pista de tiempo") == 0.0


def test_reintenta_saturacion_y_termina_bien(monkeypatch):
    p, aio = _proveedor(monkeypatch, [
        _Resp(503, '{"type":"server_error","message":"[ttft_admission_refused] the cell is at capacity; retry shortly (after 2 s)"}'),
        _Resp(429, '{"error":"rate limited"}'),
        _ok("respuesta final"),
    ])
    r = _chat(p)
    assert r.content == "respuesta final"
    assert p._client.llamadas == 3  # type: ignore[attr-defined]
    assert len(aio.dormido) == 2
    assert aio.dormido[0] >= 4.0   # respeta "after 2 s" + margen
    assert aio.dormido[1] >= 5.0   # backoff propio


def test_400_unavailable_no_se_reintenta(monkeypatch):
    """Un modelo dado de baja es permanente: reintentar solo hace perder tiempo."""
    p, aio = _proveedor(monkeypatch, [_Resp(400, '{"error":"Model is unavailable"}')])
    with pytest.raises(RuntimeError) as exc:
        _chat(p)
    assert "HTTP 400" in str(exc.value)
    assert p._client.llamadas == 1  # type: ignore[attr-defined]
    assert aio.dormido == []


def test_agota_los_reintentos_y_falla_con_el_cuerpo(monkeypatch):
    p, aio = _proveedor(monkeypatch, [
        _Resp(503, '{"message":"at capacity"}') for _ in range(10)
    ])
    with pytest.raises(RuntimeError) as exc:
        _chat(p)
    assert "at capacity" in str(exc.value)
    assert p._client.llamadas == p.chat_retries  # type: ignore[attr-defined]
    assert len(aio.dormido) == p.chat_retries - 1


def test_la_espera_no_se_dispara_aunque_el_upstream_pida_horas(monkeypatch):
    p, aio = _proveedor(monkeypatch, [
        _Resp(503, '{"message":"at capacity; retry shortly (after 3600 s)"}'),
        _ok(),
    ])
    _chat(p)
    assert aio.dormido and aio.dormido[0] <= 92.0
