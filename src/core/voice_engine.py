"""
Nexus Voice Engine — TTS (Piper) + STT (Faster-Whisper) + Push-to-Talk.

Full duplex voice system:
  - TTS: Piper ONNX local models (Spanish, English, etc.)
  - STT: Faster-Whisper local transcription (tiny/base/small/medium/large)
  - Push-to-Talk: record mic while key held, transcribe on release

Singleton: get_engine()
"""
import os
import wave
import json
import io
import tempfile
import threading
import logging
import numpy as np
from pathlib import Path
from typing import Optional, Callable

logger = logging.getLogger(__name__)

VOICE_MODELS_DIR = Path(__file__).parent.parent.parent / "voice_models" / "piper"
WHISPER_MODEL_SIZE = os.environ.get("NEXUS_WHISPER_MODEL", "base")

_engine = None
_lock = threading.Lock()


# =============================================================================
# VoiceEngine (TTS + STT)
# =============================================================================

class VoiceEngine:
    """Unified voice engine with TTS (Piper) and STT (Faster-Whisper)."""

    def __init__(self):
        # TTS state
        self.available = False
        self.model = None
        self.voice_name = "es_MX-claude-high"
        self.model_path = None
        self.config_path = None
        self.sample_rate = 22050

        # STT state
        self.stt_available = False
        self.whisper_model = None
        self.whisper_model_size = WHISPER_MODEL_SIZE

        # Push-to-talk state
        self._recording = False
        self._audio_buffer = []
        self._record_thread = None

        self._init_tts()
        self._init_stt()

    # -------------------------------------------------------------------------
    # TTS (Piper)
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
            logger.info(f"TTS ready: {self.voice_name} ({self.sample_rate}Hz)")
        except Exception as e:
            logger.warning(f"TTS init failed: {e}")
            self.available = False

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
        chunks = self.synthesize(text)
        if not chunks:
            return None
        if out_path is None:
            out_path = os.path.join(tempfile.gettempdir(), f"nexus_voice_{abs(hash(text)) % 100000}.wav")
        with wave.open(str(out_path), "w") as wav:
            wav.setnchannels(chunks[0].sample_channels)
            wav.setsampwidth(chunks[0].sample_width)
            wav.setframerate(chunks[0].sample_rate)
            for chunk in chunks:
                wav.writeframes(chunk.audio_int16_bytes)
        return out_path

    def speak_bytes(self, text):
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
    # STT (Faster-Whisper)
    # -------------------------------------------------------------------------

    def _init_stt(self):
        try:
            from faster_whisper import WhisperModel
            self.whisper_model = WhisperModel(self.whisper_model_size, device="cpu", compute_type="int8")
            self.stt_available = True
            logger.info(f"STT ready: faster-whisper {self.whisper_model_size}")
        except Exception as e:
            logger.warning(f"STT init failed: {e}")
            self.stt_available = False

    def transcribe_file(self, audio_path: str, language: str = "es") -> dict:
        """Transcribe an audio file. Returns {text, language, segments, duration}."""
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

    def transcribe_bytes(self, audio_bytes: bytes, language: str = "es") -> dict:
        """Transcribe audio from raw bytes (WAV)."""
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            return self.transcribe_file(tmp_path, language=language)
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # Push-to-Talk (record mic → transcribe)
    # -------------------------------------------------------------------------

    def start_recording(self) -> bool:
        """Start recording from microphone. Returns True if started."""
        if self._recording:
            return False
        try:
            import sounddevice as sd
        except ImportError:
            logger.error("sounddevice not installed — cannot record")
            return False

        self._recording = True
        self._audio_buffer = []

        def _record():
            try:
                import sounddevice as sd
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
        return {
            "tts_available": self.available,
            "tts_voice": self.voice_name if self.available else None,
            "tts_sample_rate": self.sample_rate if self.available else None,
            "stt_available": self.stt_available,
            "stt_model": self.whisper_model_size if self.stt_available else None,
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
