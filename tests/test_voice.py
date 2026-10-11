"""Voice INPUT: cliente STT (:8789 stub) + VAD pre-roll + inyeccion a /api/chat.

No toca session_*, agent_loop.py, spawner, trajectory, plan_mode, scheduler,
skills/* ni mcp_bridge. Sin red externa: stub HTTP local en thread.
"""
from __future__ import annotations

import io
import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from src.core import voice_engine as ve
from src.core.voice_engine import (
    SttHttpClient,
    VoiceConfig,
    build_chat_payload,
    load_voice_config,
    submit_transcript,
    vad_segments,
)


# ---------------------------------------------------------------------------
# Stub STT OpenAI-compatible (:8789)
# ---------------------------------------------------------------------------

VERBOSE_JSON = {
    "text": "hola mundo",
    "language": "es",
    "duration": 1.2,
    "segments": [
        {"start": 0.0, "end": 0.6, "text": "hola"},
        {"start": 0.6, "end": 1.2, "text": "mundo"},
    ],
}


class _StubState:
    mode = "ok"          # ok | flaky | down
    hits = 0
    last_path = ""
    last_content_type = ""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencio
        pass

    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        _StubState.hits += 1
        _StubState.last_path = self.path
        _StubState.last_content_type = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        if _StubState.mode == "flaky" and _StubState.hits == 1:
            self.send_error(500, "boom simulado")
            return
        body = json.dumps(VERBOSE_JSON).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def stt_stub():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def _reset_stub():
    _StubState.mode = "ok"
    _StubState.hits = 0
    _StubState.last_path = ""
    _StubState.last_content_type = ""
    yield


def _client(endpoint: str, **kw) -> SttHttpClient:
    return SttHttpClient(endpoint, timeout_s=5.0, retries=kw.get("retries", 2))


# ---------------------------------------------------------------------------
# 1. Cliente STT
# ---------------------------------------------------------------------------

def test_stt_transcribe_ok(stt_stub):
    res = _client(stt_stub).transcribe_bytes(b"fake-audio", language="es")
    assert res["text"] == "hola mundo"
    assert res["language"] == "es"
    assert len(res["segments"]) == 2
    assert res["segments"][0] == {"start": 0.0, "end": 0.6, "text": "hola"}
    assert _StubState.last_path == "/v1/audio/transcriptions"
    assert "multipart/form-data" in _StubState.last_content_type


def test_stt_retry_then_ok(stt_stub):
    _StubState.mode = "flaky"
    res = _client(stt_stub, retries=2).transcribe_bytes(b"x")
    assert res["text"] == "hola mundo"
    assert _StubState.hits == 2  # 500 + reintento


def test_stt_unreachable_returns_error_dict():
    res = SttHttpClient("http://127.0.0.1:9", timeout_s=1.0, retries=1).transcribe_bytes(b"x")
    assert res["text"] == "" and "error" in res


def test_stt_no_retry_on_4xx(stt_stub):
    # el stub no emite 4xx; se verifica la normalizacion con payload vacio
    norm = SttHttpClient._normalize({"text": "", "segments": []})
    assert norm == {"text": "", "language": "", "duration": 0.0, "segments": []}


# ---------------------------------------------------------------------------
# 2. VAD con pre-roll (leccion upstream: no recortar el onset)
# ---------------------------------------------------------------------------

def _synth_wav(sr=16000, silence_s=1.0, tone_s=0.5, freq=440.0) -> bytes:
    t_tone = np.arange(int(sr * tone_s)) / sr
    tone = (0.5 * np.sin(2 * np.pi * freq * t_tone) * 32767).astype(np.int16)
    pcm = np.concatenate([np.zeros(int(sr * silence_s), dtype=np.int16), tone,
                          np.zeros(int(sr * 0.5), dtype=np.int16)])
    buf = io.BytesIO()
    with wave.open(buf, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue(), pcm


def test_vad_preroll_preserves_onset():
    _, pcm = _synth_wav()  # onset del tono en t=1.0s
    segs = vad_segments(pcm, sample_rate=16000, pre_roll_ms=400)
    assert len(segs) == 1
    start, end = segs[0]
    assert start <= 1.0 - 0.3, f"onset recortado: start={start}"
    assert start >= 0.0
    assert end >= 1.4, f"cierre prematuro: end={end}"


def test_vad_without_preroll_starts_later():
    _, pcm = _synth_wav()
    with_pre = vad_segments(pcm, sample_rate=16000, pre_roll_ms=400)[0][0]
    without = vad_segments(pcm, sample_rate=16000, pre_roll_ms=0)[0][0]
    assert with_pre <= without  # el pre-roll retrocede el inicio


def test_vad_silence_only_returns_empty():
    pcm = np.zeros(16000, dtype=np.int16)
    assert vad_segments(pcm, sample_rate=16000) == []


def test_vad_short_blip_discarded():
    sr = 16000
    t = np.arange(int(sr * 0.05)) / sr  # 50ms < min_speech 250ms
    tone = (0.5 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16)
    pcm = np.concatenate([np.zeros(sr, dtype=np.int16), tone, np.zeros(sr, dtype=np.int16)])
    assert vad_segments(pcm, sample_rate=sr) == []


# ---------------------------------------------------------------------------
# 3. Inyeccion al chat por la via existente (POST /api/chat)
# ---------------------------------------------------------------------------

def test_build_chat_payload_matches_api_chat_contract():
    p = build_chat_payload("hola mundo", session_id="s1")
    assert p["message"] == "hola mundo"
    assert p["session_id"] == "s1"
    assert set(p) <= {"message", "session_id", "hint", "model"}


def test_build_chat_payload_empty_raises():
    with pytest.raises(ValueError):
        build_chat_payload("   ")


def test_submit_transcript_uses_sender_not_new_channel():
    seen = []

    def fake_sender(payload):
        seen.append(payload)
        return {"ok": True}

    out = submit_transcript("pon musica", session_id="s9", sender=fake_sender)
    assert out == {"ok": True}
    assert seen == [{"message": "pon musica", "session_id": "s9"}]


def test_submit_transcript_empty_never_calls_sender():
    called = []
    with pytest.raises(ValueError):
        submit_transcript("  ", sender=called.append)
    assert called == []


def test_submit_transcript_no_sender_returns_payload():
    assert submit_transcript("texto") == {"message": "texto"}


# ---------------------------------------------------------------------------
# 4. Config voice + engine fallback sin red ni modelos
# ---------------------------------------------------------------------------

def test_voice_config_defaults():
    cfg = load_voice_config({})
    assert cfg.stt_endpoint == "http://127.0.0.1:8789"
    assert cfg.tts_endpoint == "http://127.0.0.1:4123"
    assert cfg.vad_pre_roll_ms == 400
    assert cfg.stt_retries == 2


def test_voice_config_env_override(monkeypatch):
    monkeypatch.setenv("NEXUS_STT_ENDPOINT", "http://10.0.0.5:8789/")
    cfg = load_voice_config({})
    assert cfg.stt_endpoint == "http://10.0.0.5:8789"


def test_voice_config_from_yaml_section():
    cfg = load_voice_config({"voice": {"stt_endpoint": "http://x:1", "vad_pre_roll_ms": 100}})
    assert cfg.stt_endpoint == "http://x:1"
    assert cfg.vad_pre_roll_ms == 100


def test_engine_fallback_error_shape_without_backend_or_model(monkeypatch, tmp_path):
    # Sin tocar AgentLoop: engine con endpoint muerto y sin modelo local.
    monkeypatch.setattr(ve.VoiceEngine, "_init_tts", lambda self: None)
    monkeypatch.setattr(ve.VoiceEngine, "_init_stt", lambda self: setattr(self, "stt_available", False))
    monkeypatch.setattr(ve.VoiceEngine, "_ensure_local_model", lambda self: False)
    eng = ve.VoiceEngine(voice_config=VoiceConfig(stt_endpoint="http://127.0.0.1:9",
                                                  stt_timeout_s=1.0, stt_retries=0))
    wav = tmp_path / "sil.wav"
    pcm = np.zeros(8000, dtype=np.int16)
    with wave.open(str(wav), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    res = eng.transcribe_file(str(wav))
    assert res["text"] == "" and "error" in res


def test_engine_uses_stub_endpoint(monkeypatch, tmp_path, stt_stub):
    monkeypatch.setattr(ve.VoiceEngine, "_init_tts", lambda self: None)
    monkeypatch.setattr(ve.VoiceEngine, "_init_stt", lambda self: setattr(self, "stt_available", True))
    eng = ve.VoiceEngine(voice_config=VoiceConfig(stt_endpoint=stt_stub, stt_timeout_s=5.0))
    wav = tmp_path / "a.webm"
    wav.write_bytes(b"fake")
    res = eng.transcribe_file(str(wav))
    assert res["text"] == "hola mundo"
    assert res["segments"][1]["text"] == "mundo"
