"""describe_image debe hablar el API real de Ollama y elegir un modelo que vea.

Dos bugs encontrados con evidencia (no por lectura):

1. El bridge posteaba a `/api/vision/process`, endpoint que Ollama no tiene
   (404 verificado). La tool devolvía success=False siempre.
2. `pick_model` elegía `gemma4:12b` porque estaba primero en la lista de
   preferencias, y Ollama responde 400 "model does not support multimodal
   requests". Había qwen2.5vl:7b instalado y la tool igual fallaba.
"""
from __future__ import annotations

import json

import pytest


class _Respuesta:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class _ClienteFalso:
    def __init__(self, respuestas):
        self.respuestas = list(respuestas)
        self.llamadas: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        self.llamadas.append({"url": url, "json": json or {}})
        if not self.respuestas:
            raise AssertionError("llamada de más: no debía reintentar")
        return self.respuestas.pop(0)


def _pick_model_simple(available, prefer=(), exclude_embed=True):
    """Misma semántica que environment.pick_model, sin tocar la red."""
    for p in prefer:
        base = p.split(":")[0]
        for a in available:
            if a.split(":")[0].startswith(base):
                return a
    return available[0] if available else None


@pytest.fixture()
def bridge(monkeypatch):
    from src.bridges import mcp_bridge_server as m

    monkeypatch.setattr("src.core.environment.pick_model", _pick_model_simple)
    return m


def _enchufar(monkeypatch, instalados, respuestas):
    from src.bridges import mcp_bridge_server as m

    monkeypatch.setattr("src.core.environment.list_ollama_models", lambda *a, **k: list(instalados))
    cliente = _ClienteFalso(respuestas)
    monkeypatch.setattr(m.httpx, "AsyncClient", lambda *a, **k: cliente)
    return cliente


def test_pega_en_api_generate_y_limpia_el_prefijo(bridge, monkeypatch):
    import asyncio

    cliente = _enchufar(monkeypatch, ["qwen2.5vl:7b"],
                        [_Respuesta(200, {"response": "rojo"})])
    out = json.loads(asyncio.run(bridge.describe_image("data:image/png;base64,AAAA", "¿Qué color ves?")))

    assert out["success"] is True
    assert out["response"] == "rojo"
    llamada = cliente.llamadas[0]
    assert llamada["url"].endswith("/api/generate")
    assert "vision/process" not in llamada["url"]
    assert llamada["json"]["images"] == ["AAAA"]
    assert llamada["json"]["stream"] is False


def test_no_elige_un_modelo_sin_vision_aunque_este_primero(bridge, monkeypatch):
    import asyncio

    # gemma4 instalado y con prioridad alta: no puede ser candidato.
    cliente = _enchufar(monkeypatch, ["gemma4:12b", "qwen2.5vl:7b"],
                        [_Respuesta(200, {"response": "rojo"})])
    out = json.loads(asyncio.run(bridge.describe_image("AAAA")))

    assert out["model"] == "qwen2.5vl:7b"
    assert [c["json"]["model"] for c in cliente.llamadas] == ["qwen2.5vl:7b"]


def test_reintenta_con_otro_modelo_de_vision(bridge, monkeypatch):
    import asyncio

    cliente = _enchufar(
        monkeypatch,
        ["qwen2.5vl:7b", "qwen3-vl-8b"],
        [_Respuesta(400, {"error": "Multimodal data provided, but model does not support multimodal requests."}),
         _Respuesta(200, {"response": "rojo"})],
    )
    out = json.loads(asyncio.run(bridge.describe_image("AAAA")))

    assert out["success"] is True
    assert out["model"] == "qwen3-vl-8b"
    assert [c["json"]["model"] for c in cliente.llamadas] == ["qwen2.5vl:7b", "qwen3-vl-8b"]


def test_error_que_no_es_multimodal_no_reintenta(bridge, monkeypatch):
    import asyncio

    cliente = _enchufar(monkeypatch, ["qwen2.5vl:7b", "qwen3-vl-8b"],
                        [_Respuesta(404, {"error": "model not found"})])
    out = json.loads(asyncio.run(bridge.describe_image("AAAA")))

    assert out["success"] is False
    assert "404" in out["error"]
    assert len(cliente.llamadas) == 1


def test_vision_gem_v2_ya_no_existe(bridge):
    """Era un módulo de 242 líneas que nadie importaba; la visión viva es
    describe_image (tool) + los adjuntos del chat (/api/upload)."""
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("src.tools.vision_gem_v2")
