"""
Nexus Voice Engine — voice INPUT (mic → texto → chat) + TTS legacy.

Pipeline de entrada (alcance actual):
  mic/navegador → POST /api/voice/stt → SttHttpClient → STT faster-whisper
  OpenAI-compatible en {stt_endpoint}/v1/audio/transcriptions
  (multipart: file + response_format=verbose_json → segments[{start,end,text}])
  → transcript → build_chat_payload/submit_transcript → POST /api/chat
  (AgentLoop, la via existente; NO se crea canal nuevo).

Leccion upstream (deepseek-harness, packages experimentales de voz):
  "restore the onset a cold VAD drops" — un VAD en frio recorta el inicio
  del habla. Fix aplicado aqui: pre-roll (vad_pre_roll_ms, default 400ms):
  cada segmento detectado retrocede N ms para conservar el onset, +
  hangover (cierre con retardo) + min_speech_ms (anti-blips).
  Todo con stdlib + numpy (sin webrtcvad/sounddevice obligatorios).

Fallbacks:
  - Si el endpoint STT no responde: reintentos con backoff; luego modelo
    faster-whisper local en proceso (si esta instalado); si no, error.
  - TTS Piper local se conserva tal cual (legacy). tts_endpoint queda
    reservado/configurable para el TTS Chatterbox (:4123, FUERA de alcance).

Config: seccion `voice:` en config.yaml (ver config.schema.json) + env:
  NEXUS_STT_ENDPOINT, NEXUS_STT_TIMEOUT_S, NEXUS_STT_RETRIES,
  NEXUS_VOICE_LANG, NEXUS_TTS_ENDPOINT.

Singleton: get_engine()
"""

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

VOICE_MODELS_DIR = Path(__file__).parent.parent.parent / "voice_models" / "piper"
WHISPER_MODEL_SIZE = os.environ.get("NEXUS_WHISPER_MODEL", "base")

# Path /api/chat (AgentLoop): la via existente de inyeccion. No inventar otra.
CHAT_PATH = "/api/chat"

_engine = None
_lock = threading.Lock()


# =============================================================================
# Config
# =============================================================================

@dataclass
class VoiceConfig:
    """Parametros de voz. Los VAD estan documentados por la leccion upstream."""

    stt_endpoint: str = "http://127.0.0.1:8789"
    stt_timeout_s: float = 30.0
    stt_retries: int = 2
    language: str = "es"
    # --- VAD (energia RMS normalizada, puro numpy) ---
    vad_sample_rate: int = 16000
    vad_frame_ms: int = 30          # ventana de analisis
    vad_threshold: float = 0.02     # RMS normalizado >= esto = habla
    vad_pre_roll_ms: int = 400      # FIX onset: retrocede el inicio N ms
    vad_hangover_ms: int = 300      # retardo antes de cerrar segmento
    vad_min_speech_ms: int = 250    # segmentos mas cortos se descartan
    # --- TTS (reservado; TTS full fuera de alcance) ---
    tts_endpoint: str = "http://127.0.0.1:4123"

    def vad_kwargs(self) -> dict:
        return {
            "sample_rate": self.vad_sample_rate,
            "frame_ms": self.vad_frame_ms,
            "threshold": self.vad_threshold,
            "pre_roll_ms": self.vad_pre_roll_ms,
            "hangover_ms": self.vad_hangover_ms,
            "min_speech_ms": self.vad_min_speech_ms,
        }


def load_voice_config(cfg: Optional[dict] = None) -> VoiceConfig:
    """Lee seccion `voice:` del config dict + overrides por env. Nunca lanza."""
    v: dict = {}
    try:
        if cfg is None:
            from src.core.config import load_config
            cfg = load_config()
        if isinstance(cfg, dict):
            v = dict(cfg.get("voice") or {})
    except Exception as e:
        logger.warning(f"voice config: usando defaults ({e})")

    def _env(key: str) -> Optional[str]:
        val = os.environ.get(key)
        return val if val not in (None, "") else None

    def _get(name: str, default: Any, cast: Callable = str) -> Any:
        raw = v.get(name, default)
        try:
            return cast(raw)
        except Exception:
            return default

    stt_endpoint = (_env("NEXUS_STT_ENDPOINT") or str(
        v.get("stt_endpoint", VoiceConfig.stt_endpoint)
    )).rstrip("/")
    tts_endpoint = (_env("NEXUS_TTS_ENDPOINT") or str(
        v.get("tts_endpoint", VoiceConfig.tts_endpoint)
    )).rstrip("/")
    timeout_raw = _env("NEXUS_STT_TIMEOUT_S")
    retries_raw = _env("NEXUS_STT_RETRIES")
    lang = _env("NEXUS_VOICE_LANG") or str(v.get("language", "es"))
    try:
        timeout = float(timeout_raw) if timeout_raw else float(v.get("stt_timeout_s", 30.0))
    except ValueError:
        timeout = 30.0
    try:
        retries = int(retries_raw) if retries_raw else int(v.get("stt_retries", 2))
    except ValueError:
        retries = 2
    return VoiceConfig(
        stt_endpoint=stt_endpoint,
        stt_timeout_s=timeout,
        stt_retries=max(0, retries),
        language=lang,
        vad_sample_rate=_get("vad_sample_rate", 16000, int),
        vad_frame_ms=_get("vad_frame_ms", 30, int),
        vad_threshold=_get("vad_threshold", 0.02, float),
        vad_pre_roll_ms=_get("vad_pre_roll_ms", 400, int),
        vad_hangover_ms=_get("vad_hangover_ms", 300, int),
        vad_min_speech_ms=_get("vad_min_speech_ms", 250, int),
        tts_endpoint=tts_endpoint,
    )


# =============================================================================
# VAD con pre-roll (puro numpy; fix del onset recortado por VAD en frio)
# =============================================================================

def vad_segments(
    pcm: "Any",
    sample_rate: int = 16000,
    frame_ms: int = 30,
    threshold: float = 0.02,
    pre_roll_ms: int = 400,
    hangover_ms: int = 300,
    min_speech_ms: int = 250,
) -> list:
    """Detecta segmentos de habla en PCM int16 mono.

    Devuelve [(start_s, end_s), ...] con el inicio retrocedido pre_roll_ms
    (conserva el onset que un VAD en frio recortaria) y cierre con hangover.
    Sin dependencias mas alla de numpy.
    """
    import numpy as np

    x = np.asarray(pcm).flatten()
    if x.size == 0:
        return []
    if x.dtype != np.int16:
        x = np.clip(x, -1.0, 1.0).astype(np.float32) if x.dtype.kind == "f" else np.asarray(x, dtype=np.int32)
        if x.dtype.kind != "f":
            x = (x / 32768.0).astype(np.float32)
    else:
        x = (x.astype(np.float32) / 32768.0)
    frame_len = max(1, int(sample_rate * frame_ms / 1000))
    n_frames = int(np.ceil(len(x) / frame_len))
    if n_frames == 0:
        return []
    padded = np.pad(x, (0, n_frames * frame_len - len(x)))
    frames = padded.reshape(n_frames, frame_len)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    is_speech = rms >= threshold

    pre_roll_frames = int(round(pre_roll_ms / frame_ms))
    hangover_frames = int(round(hangover_ms / frame_ms))
    min_frames = max(1, int(round(min_speech_ms / frame_ms)))

    segments: list = []
    in_seg = False
    seg_start = 0
    silence_run = 0
    speech_frames = 0  # habla real (sin pre-roll/hangover): para min_speech
    for i, speech in enumerate(is_speech):
        if not in_seg:
            if speech:
                in_seg = True
                # FIX onset: arrancar pre_roll antes del primer frame con voz.
                seg_start = max(0, i - pre_roll_frames)
                silence_run = 0
                speech_frames = 1
        else:
            if speech:
                silence_run = 0
                speech_frames += 1
            else:
                silence_run += 1
                if silence_run > hangover_frames:
                    seg_end_frame = i - silence_run + 1 + hangover_frames
                    if speech_frames >= min_frames:
                        segments.append((seg_start * frame_ms / 1000.0, seg_end_frame * frame_ms / 1000.0))
                    in_seg = False
                    silence_run = 0
    if in_seg:
        seg_end_frame = n_frames
        if speech_frames >= min_frames:
            segments.append((seg_start * frame_ms / 1000.0, seg_end_frame * frame_ms / 1000.0))
    return [(round(s, 3), round(e, 3)) for s, e in segments]


def read_wav_mono16(path: str, target_sr: int = 16000) -> tuple:
    """Lee WAV y devuelve (pcm_int16_mono, sample_rate). Sin resampleo."""
    import numpy as np

    with wave.open(path, "rb") as wav:
        n_ch = wav.getnchannels()
        sampwidth = wav.getsampwidth()
        sr = wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if sampwidth == 2:
        pcm = np.frombuffer(raw, dtype=np.int16)
    elif sampwidth == 1:
        pcm = ((np.frombuffer(raw, dtype=np.uint8).astype(np.int32) - 128) * 256).astype(np.int16)
    else:
        raise ValueError(f"sampwidth no soportado: {sampwidth}")
    if n_ch > 1:
        pcm = pcm.reshape(-1, n_ch).mean(axis=1).astype(np.int16)
    if sr != target_sr:
        raise ValueError(f"sample_rate {sr} != {target_sr} (convertir antes)")
    return pcm, sr


# =============================================================================
# Cliente STT HTTP (OpenAI-compatible: /v1/audio/transcriptions)
# =============================================================================

def _build_multipart(file_bytes: bytes, filename: str, fields: dict) -> tuple:
    boundary = f"----nexusvoice{int(time.time() * 1000)}"
    buf = io.BytesIO()
    for k, val in fields.items():
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        buf.write(f"{val}\r\n".encode())
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode()
    )
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(file_bytes)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


class SttHttpClient:
    """Cliente del STT faster-whisper OpenAI-compatible (verbose_json)."""

    def __init__(self, endpoint: str, timeout_s: float = 30.0, retries: int = 2):
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = max(0, retries)

    @property
    def url(self) -> str:
        return f"{self.endpoint}/v1/audio/transcriptions"

    def transcribe_bytes(
        self, audio_bytes: bytes, filename: str = "audio.webm", language: str = "es"
    ) -> dict:
        """POST multipart → {text, language, duration, segments} o {error, text}. stdlib."""
        fields = {"response_format": "verbose_json", "language": language}
        body, content_type = _build_multipart(audio_bytes, filename, fields)
        last_err: Optional[str] = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(
                    self.url, data=body, headers={"Content-Type": content_type}, method="POST"
                )
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                return self._normalize(payload)
            except urllib.error.HTTPError as e:
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    detail = ""
                last_err = f"stt http {e.code}: {detail}"
                if 400 <= e.code < 500:
                    break  # error de cliente: reintentar no ayuda
            except Exception as e:  # URLError/timeout/conexion
                last_err = f"stt no alcanzable ({type(e).__name__}: {e})".strip()[:300]
            if attempt < self.retries:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        return {"error": last_err or "stt failed", "text": ""}

    def transcribe_file(self, audio_path: str, language: str = "es") -> dict:
        with open(audio_path, "rb") as f:
            data = f.read()
        return self.transcribe_bytes(data, filename=Path(audio_path).name, language=language)

    @staticmethod
    def _normalize(payload: dict) -> dict:
        """verbose_json → {text, language, duration, segments[{start,end,text}]}."""
        segments = []
        for seg in payload.get("segments") or []:
            try:
                segments.append(
                    {
                        "start": round(float(seg.get("start", 0.0)), 2),
                        "end": round(float(seg.get("end", 0.0)), 2),
                        "text": str(seg.get("text", "")).strip(),
                    }
                )
            except Exception:
                continue
        text = str(payload.get("text", "")).strip()
        if not text and segments:
            text = " ".join(s["text"] for s in segments).strip()
        return {
            "text": text,
            "language": payload.get("language", ""),
            "duration": payload.get("duration", 0.0),
            "segments": segments,
        }


# =============================================================================
# Inyeccion al chat (via existente: POST /api/chat → AgentLoop)
# =============================================================================

def build_chat_payload(
    transcript: str,
    session_id: Optional[str] = None,
    hint: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Payload identico al que la UI envia a POST /api/chat. Lanza si vacio."""
    text = (transcript or "").strip()
    if not text:
        raise ValueError("transcript vacio — nada que inyectar")
    payload: dict = {"message": text}
    if session_id:
        payload["session_id"] = session_id
    if hint:
        payload["hint"] = hint
    if model:
        payload["model"] = model
    return payload


def submit_transcript(
    transcript: str,
    session_id: Optional[str] = None,
    hint: Optional[str] = None,
    model: Optional[str] = None,
    sender: Optional[Callable[[dict], Any]] = None,
    backend_url: Optional[str] = None,
    timeout_s: float = 90.0,
) -> Any:
    """Inyecta el transcript por la via existente (POST {backend}/api/chat).

    - sender(payload) (inyectable, para tests): se llama con el payload.
    - backend_url: POST HTTP real a {backend_url}/api/chat.
    - sin ambos: devuelve el payload sin enviar (el llamador lo POSTea).
    """
    payload = build_chat_payload(transcript, session_id, hint, model)
    if sender is not None:
        return sender(payload)
    if backend_url:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            backend_url.rstrip("/") + CHAT_PATH,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    return payload


# =============================================================================
# VoiceEngine (TTS legacy + STT HTTP-first con fallback local + VAD + PTT)
# =============================================================================

class VoiceEngine:
    """Motor de voz unificado. API compatible con la version anterior."""

    def __init__(self, voice_config: Optional[VoiceConfig] = None):
        # TTS state (Piper legacy; fallback SAPI nativo)
        self.available = False
        self.tts_backend: Optional[str] = None  # "piper" | "sapi"
        self.model = None
        self.voice_name = "es_MX-claude-high"
        self.model_path = None
        self.config_path = None
        self.sample_rate = 22050

        # STT state
        self.stt_available = False
        self.whisper_model = None
        self.whisper_model_size = WHISPER_MODEL_SIZE
        self.voice_config = voice_config or load_voice_config()
        self.stt_client = SttHttpClient(
            endpoint=self.voice_config.stt_endpoint,
            timeout_s=self.voice_config.stt_timeout_s,
            retries=self.voice_config.stt_retries,
        )

        # Push-to-talk state
        self._recording = False
        self._audio_buffer = []
        self._record_thread = None

        self._init_tts()
        self._init_stt()

    # -------------------------------------------------------------------------
    # TTS (Piper, legacy — sin cambios)
    # -------------------------------------------------------------------------

    def _init_tts(self):
        try:
            from piper import PiperVoice
            voices = self.get_voices()
            if not voices:
                return
            quality_order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
            best = min(voices, key=lambda v: quality_order.get(v["quality"], 3))
            self.voice_name = best["name"]
            self.model_path = str(VOICE_MODELS_DIR / f"{self.voice_name}.onnx")
            self.config_path = str(VOICE_MODELS_DIR / f"{self.voice_name}.onnx.json")
            self.model = PiperVoice.load(self.model_path, config_path=self.config_path)
            with open(self.config_path, encoding="utf-8") as f:
                cfg = json.load(f)
            self.sample_rate = cfg.get("audio", {}).get("sample_rate", 22050)
            self.available = True
            self.tts_backend = "piper"
            logger.info(f"TTS ready: {self.voice_name} ({self.sample_rate}Hz)")
        except Exception as e:
            logger.warning(f"TTS init failed: {e}")
            self.available = False
            self._init_sapi()

    def _init_sapi(self):
        """Fallback TTS nativo de Windows (System.Speech): sin modelos ni descargas."""
        if os.name != "nt" or not shutil.which("powershell"):
            return
        self.tts_backend = "sapi"
        self.available = True
        self.voice_name = os.environ.get("NEXUS_TTS_VOICE") or "sapi:default"
        self.sample_rate = 22050
        logger.info(f"TTS ready: Windows SAPI ({self.voice_name})")

    def _sapi_wav(self, text: str) -> Optional[bytes]:
        """Sintetiza con System.Speech (PowerShell) y devuelve WAV en memoria.

        El texto y la ruta viajan por archivos UTF-8: nada del contenido entra
        en el script, asi que no hay interpolacion ni inyeccion de comandos.
        """
        tmp_dir = Path(tempfile.gettempdir())
        stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
        txt_path = tmp_dir / f"nexus_sapi_{stamp}.txt"
        wav_path = tmp_dir / f"nexus_sapi_{stamp}.wav"
        voice = os.environ.get("NEXUS_TTS_VOICE", "").strip()
        select = ""
        if voice:
            select = f"try {{ $s.SelectVoice('{voice.replace(chr(39), chr(39) * 2)}') }} catch {{}}"
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"{select} "
            f"$s.SetOutputToWaveFile('{str(wav_path).replace(chr(39), chr(39) * 2)}'); "
            f"$t = [IO.File]::ReadAllText('{str(txt_path).replace(chr(39), chr(39) * 2)}', [Text.Encoding]::UTF8); "
            "$s.Speak($t); $s.Dispose()"
        )
        try:
            txt_path.write_text(text, encoding="utf-8")
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode != 0 or not wav_path.exists():
                logger.warning(f"SAPI TTS fallo: {(proc.stderr or b'')[:300]!r}")
                return None
            data = wav_path.read_bytes()
            return data if data[:4] == b"RIFF" else None
        except Exception as e:
            logger.warning(f"SAPI TTS error: {e}")
            return None
        finally:
            for p in (txt_path, wav_path):
                try:
                    p.unlink()
                except Exception:
                    pass

    def get_voices(self):
        voices = []
        if not VOICE_MODELS_DIR.exists():
            return voices
        for f in sorted(VOICE_MODELS_DIR.glob("*.onnx")):
            name = f.stem
            cfg_f = f.with_suffix(".onnx.json")
            quality = "unknown"
            if cfg_f.exists():
                try:
                    data = json.loads(cfg_f.read_text(encoding="utf-8"))
                    quality = data.get("audio", {}).get("quality", "unknown")
                except Exception:
                    pass
            voices.append({"name": name, "quality": quality})
        return voices

    def set_voice(self, name):
        from piper import PiperVoice
        onnx = VOICE_MODELS_DIR / f"{name}.onnx"
        cfg = VOICE_MODELS_DIR / f"{name}.onnx.json"
        if onnx.exists() and cfg.exists():
            self.model_path = str(onnx)
            self.config_path = str(cfg)
            self.voice_name = name
            self.model = PiperVoice.load(self.model_path, config_path=self.config_path)
            with open(self.config_path, encoding="utf-8") as f:
                data = json.load(f)
            self.sample_rate = data.get("audio", {}).get("sample_rate", 22050)
            return True
        return False

    def synthesize(self, text):
        if not self.available or not self.model:
            return None
        chunks = []
        for chunk in self.model.synthesize(text):
            chunks.append(chunk)
        if not chunks:
            return None
        return chunks

    def speak(self, text, out_path=None):
        if out_path is None:
            out_path = os.path.join(tempfile.gettempdir(), f"nexus_voice_{abs(hash(text)) % 100000}.wav")
        if self.tts_backend == "sapi":
            data = self._sapi_wav(text)
            if not data:
                return None
            Path(out_path).write_bytes(data)
            return out_path
        chunks = self.synthesize(text)
        if not chunks:
            return None
        with wave.open(str(out_path), "w") as wav:
            wav.setnchannels(chunks[0].sample_channels)
            wav.setsampwidth(chunks[0].sample_width)
            wav.setframerate(chunks[0].sample_rate)
            for chunk in chunks:
                wav.writeframes(chunk.audio_int16_bytes)
        return out_path

    def speak_bytes(self, text):
        if self.tts_backend == "sapi":
            return self._sapi_wav(text)
        chunks = self.synthesize(text)
        if not chunks:
            return None
        buf = io.BytesIO()
        with wave.open(buf, "w") as wav:
            wav.setnchannels(chunks[0].sample_channels)
            wav.setsampwidth(chunks[0].sample_width)
            wav.setframerate(chunks[0].sample_rate)
            for chunk in chunks:
                wav.writeframes(chunk.audio_int16_bytes)
        return buf.getvalue()

    # -------------------------------------------------------------------------
    # STT: HTTP-first (:8789) + fallback local faster-whisper
    # -------------------------------------------------------------------------

    def _init_stt(self):
        # El STT principal es el servicio HTTP (:8789). El modelo local es
        # solo fallback y se carga bajo demanda (lazy) para no descargar
        # nada en el arranque.
        try:
            from faster_whisper import WhisperModel  # noqa: F401 (sondeo)
            self.stt_available = True
            logger.info("STT: endpoint %s (+ fallback local %s)", self.stt_client.url, self.whisper_model_size)
        except Exception as e:
            logger.warning(f"STT fallback local no disponible: {e}")
            self.stt_available = False

    def _ensure_local_model(self) -> bool:
        if self.whisper_model is not None:
            return True
        try:
            from faster_whisper import WhisperModel
            self.whisper_model = WhisperModel(self.whisper_model_size, device="cpu", compute_type="int8")
            self.stt_available = True
            logger.info(f"STT fallback listo: faster-whisper {self.whisper_model_size}")
            return True
        except Exception as e:
            logger.warning(f"STT fallback fallo: {e}")
            return False

    def transcribe_file(self, audio_path: str, language: Optional[str] = None) -> dict:
        """Transcribe: 1) endpoint HTTP :8789, 2) fallback local. {text, ...}."""
        lang = language or self.voice_config.language
        try:
            with open(audio_path, "rb") as f:
                data = f.read()
        except OSError as e:
            return {"error": f"audio no legible: {e}", "text": ""}
        result = self.stt_client.transcribe_bytes(data, filename=Path(audio_path).name, language=lang)
        if not result.get("error"):
            return result
        logger.warning(f"STT endpoint fallo ({result['error']}); probando fallback local")
        if not self._ensure_local_model():
            return result  # error del endpoint (sin modelo local)
        return self._transcribe_local(audio_path, lang)

    def transcribe_bytes(self, audio_bytes: bytes, language: Optional[str] = None) -> dict:
        """Transcribe audio desde bytes crudos (WAV/webm)."""
        lang = language or self.voice_config.language
        result = self.stt_client.transcribe_bytes(audio_bytes, language=lang)
        if not result.get("error"):
            return result
        logger.warning(f"STT endpoint fallo ({result['error']}); probando fallback local")
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            if not self._ensure_local_model():
                return result
            return self._transcribe_local(tmp_path, lang)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _transcribe_local(self, audio_path: str, language: str) -> dict:
        """Fallback: faster-whisper en proceso. {text, language, segments, duration}."""
        if not self.stt_available or not self.whisper_model:
            return {"error": "STT not available", "text": ""}
        try:
            segments, info = self.whisper_model.transcribe(
                audio_path, language=language, beam_size=5
            )
            full_text = ""
            seg_list = []
            for seg in segments:
                full_text += seg.text
                seg_list.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                })
            return {
                "text": full_text.strip(),
                "language": info.language,
                "language_probability": round(info.language_probability, 3),
                "duration": round(info.duration, 2),
                "segments": seg_list,
            }
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return {"error": str(e), "text": ""}

    def transcribe_with_vad(self, audio_path: str, language: Optional[str] = None) -> dict:
        """VAD (con pre-roll) + STT. Agrega `vad_segments` al resultado.

        Si el WAV es 16kHz mono se segmenta; si no, se transcribe directo.
        """
        lang = language or self.voice_config.language
        vad_segs: list = []
        try:
            pcm, sr = read_wav_mono16(audio_path, target_sr=self.voice_config.vad_sample_rate)
            vad_segs = vad_segments(pcm, **self.voice_config.vad_kwargs())
        except Exception as e:
            logger.debug(f"VAD omitido ({e}); transcribiendo directo")
        result = self.transcribe_file(audio_path, language=lang)
        result["vad_segments"] = [{"start": s, "end": e} for s, e in vad_segs]
        return result

    # -------------------------------------------------------------------------
    # Push-to-Talk (record mic → transcribe). Sin sounddevice: error claro.
    # -------------------------------------------------------------------------

    def start_recording(self) -> bool:
        """Start recording from microphone. Returns True if started."""
        if self._recording:
            return False
        try:
            import sounddevice as sd  # availability check; _record lo usa por closure
        except ImportError:
            logger.error("sounddevice not installed — la captura es desde el navegador (VoiceMic.tsx)")
            return False

        self._recording = True
        self._audio_buffer = []

        def _record():
            try:
                with sd.InputStream(
                    samplerate=16000, channels=1, dtype="int16",
                    callback=self._audio_callback
                ):
                    while self._recording:
                        sd.sleep(50)
            except Exception as e:
                logger.error(f"Recording error: {e}")
                self._recording = False

        self._record_thread = threading.Thread(target=_record, daemon=True)
        self._record_thread.start()
        return True

    def _audio_callback(self, indata, frames, time_info, status):
        """Callback for sounddevice InputStream."""
        if status:
            logger.warning(f"Audio callback status: {status}")
        self._audio_buffer.append(indata.copy())

    def stop_recording(self) -> dict:
        """Stop recording and transcribe. Returns {text, ...}."""
        if not self._recording:
            return {"error": "Not recording"}
        self._recording = False
        if self._record_thread:
            self._record_thread.join(timeout=5)

        if not self._audio_buffer:
            return {"error": "No audio recorded", "text": ""}

        # Convert to WAV bytes
        import numpy as np

        audio_data = np.concatenate(self._audio_buffer, axis=0)
        wav_bytes = io.BytesIO()
        with wave.open(wav_bytes, "w") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)  # int16
            wav.setframerate(16000)
            wav.writeframes(audio_data.tobytes())

        return self.transcribe_bytes(wav_bytes.getvalue())

    @property
    def is_recording(self) -> bool:
        return self._recording

    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------

    def get_status(self) -> dict:
        # Sondeo rapido del endpoint (timeout corto) sin bloquear.
        stt_endpoint_ok: Optional[bool] = None
        try:
            req = urllib.request.Request(self.voice_config.stt_endpoint + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=2.0):
                stt_endpoint_ok = True
        except Exception:
            stt_endpoint_ok = False
        return {
            "tts_available": self.available,
            "tts_backend": self.tts_backend,
            "tts_voice": self.voice_name if self.available else None,
            "tts_sample_rate": self.sample_rate if self.available else None,
            "stt_available": self.stt_available or bool(stt_endpoint_ok),
            "stt_model": self.whisper_model_size if self.stt_available else None,
            "stt_endpoint": self.voice_config.stt_endpoint,
            "stt_endpoint_ok": stt_endpoint_ok,
            "tts_endpoint": self.voice_config.tts_endpoint,
            "is_recording": self._recording,
            "voices": self.get_voices() if self.available else [],
        }


# =============================================================================
# Singletons
# =============================================================================

def get_engine() -> VoiceEngine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = VoiceEngine()
    return _engine


def speak(text, out_path=None):
    engine = get_engine()
    if engine.available:
        return engine.speak(text, out_path)
    return None


def transcribe(audio_path, language="es"):
    engine = get_engine()
    return engine.transcribe_file(audio_path, language)
