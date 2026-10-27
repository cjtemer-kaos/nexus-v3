"""
Onboarding de Nexus v3.

Máquina de estados con consentimiento: welcome → consent → profile → connect → done.
Cada paso exige confirmación explícita del usuario antes de avanzar.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

STATE_PATH = Path(__file__).parent.parent.parent / "data" / "onboarding_state.json"


# =============================================================================
# State Machine
# =============================================================================

class OnboardingStep(str, Enum):
    WELCOME = "welcome"
    CONSENT = "consent"
    PROFILE = "profile"
    CONNECT = "connect"
    DONE = "done"


# Valid transitions: from_step -> list of allowed next steps
TRANSITIONS: Dict[OnboardingStep, List[OnboardingStep]] = {
    OnboardingStep.WELCOME: [OnboardingStep.CONSENT],
    OnboardingStep.CONSENT: [OnboardingStep.PROFILE, OnboardingStep.WELCOME],
    OnboardingStep.PROFILE: [OnboardingStep.CONNECT, OnboardingStep.CONSENT],
    OnboardingStep.CONNECT: [OnboardingStep.DONE, OnboardingStep.PROFILE],
    OnboardingStep.DONE: [],  # Terminal state
}


@dataclass
class ConsentData:
    """User consent preferences."""
    analytics: bool = False
    error_reporting: bool = False
    skill_sharing: bool = False
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class ProfileData:
    """User profile for onboarding."""
    name: str = ""
    role: str = ""              # developer, researcher, manager, etc.
    interests: List[str] = field(default_factory=list)
    experience: str = ""        # beginner, intermediate, advanced
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class ConnectData:
    """Platform connections configured during onboarding."""
    telegram: bool = False
    discord: bool = False
    github: bool = False
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


@dataclass
class OnboardingState:
    """Full onboarding state."""
    current_step: OnboardingStep = OnboardingStep.WELCOME
    consent: Optional[ConsentData] = None
    profile: Optional[ProfileData] = None
    connect: Optional[ConnectData] = None
    completed_at: str = ""
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_step": self.current_step.value,
            "consent": asdict(self.consent) if self.consent else None,
            "profile": asdict(self.profile) if self.profile else None,
            "connect": asdict(self.connect) if self.connect else None,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OnboardingState":
        consent = None
        if data.get("consent"):
            consent = ConsentData(**data["consent"])

        profile = None
        if data.get("profile"):
            profile = ProfileData(**data["profile"])

        connect = None
        if data.get("connect"):
            connect = ConnectData(**data["connect"])

        return cls(
            current_step=OnboardingStep(data.get("current_step", "welcome")),
            consent=consent,
            profile=profile,
            connect=connect,
            completed_at=data.get("completed_at", ""),
            created_at=data.get("created_at", ""),
        )


# =============================================================================
# Onboarding Manager
# =============================================================================

from dataclasses import asdict


class OnboardingManager:
    """
    Manages the onboarding state machine.

    Usage:
        ob = OnboardingManager()
        ob.start()
        ob.consent(ConsentData(analytics=True))
        ob.profile(ProfileData(name="User", role="developer"))
        ob.connect(ConnectData(telegram=True))
        # state is now DONE
    """

    def __init__(self, state_path: Optional[Path] = None):
        self.state_path = state_path or STATE_PATH
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    def _load_state(self) -> OnboardingState:
        """Load state from disk."""
        if self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                return OnboardingState.from_dict(data)
            except Exception as e:
                logger.warning(f"Failed to load onboarding state: {e}")
        return OnboardingState()

    def _save_state(self):
        """Save state to disk."""
        self.state_path.write_text(
            json.dumps(self.state.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _transition(self, target: OnboardingStep) -> bool:
        """Attempt a state transition."""
        allowed = TRANSITIONS.get(self.state.current_step, [])
        if target not in allowed:
            logger.warning(
                f"Invalid transition: {self.state.current_step.value} -> {target.value}"
            )
            return False

        self.state.current_step = target
        self._save_state()
        return True

    def start(self) -> Dict[str, Any]:
        """Start onboarding from welcome."""
        self.state = OnboardingState()
        self._save_state()
        return self.get_current_step()

    def get_current_step(self) -> Dict[str, Any]:
        """Get current step info and what's needed."""
        step = self.state.current_step

        step_info = {
            OnboardingStep.WELCOME: {
                "step": "welcome",
                "title": "Bienvenido a Nexus v3",
                "description": "Your AI ecosystem. Let's get you set up.",
                "action": "Press 'Next' to continue",
            },
            OnboardingStep.CONSENT: {
                "step": "consent",
                "title": "Privacy & Consent",
                "description": "Choose what data you're comfortable sharing to improve your experience.",
                "options": [
                    {"key": "analytics", "label": "Usage analytics", "default": False},
                    {"key": "error_reporting", "label": "Error reporting", "default": True},
                    {"key": "skill_sharing", "label": "Share created skills", "default": False},
                ],
            },
            OnboardingStep.PROFILE: {
                "step": "profile",
                "title": "Your Profile",
                "description": "Help us personalize your experience.",
                "fields": [
                    {"key": "name", "label": "Name", "type": "text", "required": True},
                    {"key": "role", "label": "Role", "type": "select", "options": ["developer", "researcher", "manager", "designer", "other"]},
                    {"key": "interests", "label": "Interests", "type": "multiselect", "options": ["coding", "research", "automation", "creative", "DevOps", "data"]},
                    {"key": "experience", "label": "Experience Level", "type": "select", "options": ["beginner", "intermediate", "advanced"]},
                ],
            },
            OnboardingStep.CONNECT: {
                "step": "connect",
                "title": "Connect Platforms",
                "description": "Optionally connect your messaging platforms.",
                "options": [
                    {"key": "telegram", "label": "Telegram", "configured": self._check_token("TELEGRAM_BOT_TOKEN")},
                    {"key": "discord", "label": "Discord", "configured": self._check_token("DISCORD_TOKEN")},
                    {"key": "github", "label": "GitHub", "configured": self._check_token("GITHUB_TOKEN")},
                ],
            },
            OnboardingStep.DONE: {
                "step": "done",
                "title": "You're All Set!",
                "description": "Onboarding completo. Nexus v3 está listo para usar.",
                "profile": asdict(self.state.profile) if self.state.profile else None,
            },
        }

        info = step_info.get(step, {})
        info["is_complete"] = step == OnboardingStep.DONE
        info["can_go_back"] = step not in (OnboardingStep.WELCOME, OnboardingStep.DONE)
        return info

    def _check_token(self, env_var: str) -> bool:
        """Check if an env var is set (without reading value)."""
        import os
        return bool(os.environ.get(env_var))

    def consent(self, data: ConsentData) -> Dict[str, Any]:
        """Submit consent: WELCOME -> CONSENT -> PROFILE."""
        self.state.consent = data
        # First advance to CONSENT step
        self._transition(OnboardingStep.CONSENT)
        # Then advance to PROFILE
        self._transition(OnboardingStep.PROFILE)
        return self.get_current_step()

    def profile(self, data: ProfileData) -> Dict[str, Any]:
        """Submit profile: CONSENT -> PROFILE -> CONNECT."""
        self.state.profile = data
        # Ensure we're at PROFILE (consent may have already moved us)
        if self.state.current_step != OnboardingStep.PROFILE:
            self._transition(OnboardingStep.PROFILE)
        self._transition(OnboardingStep.CONNECT)
        return self.get_current_step()

    def connect(self, data: ConnectData) -> Dict[str, Any]:
        """Submit connections: PROFILE -> CONNECT -> DONE."""
        self.state.connect = data
        self.state.completed_at = datetime.now(timezone.utc).isoformat()
        # Ensure we're at CONNECT
        if self.state.current_step != OnboardingStep.CONNECT:
            self._transition(OnboardingStep.CONNECT)
        self._transition(OnboardingStep.DONE)
        return self.get_current_step()

    def go_back(self) -> Dict[str, Any]:
        """Go back to the previous step."""
        back_map = {
            OnboardingStep.CONSENT: OnboardingStep.WELCOME,
            OnboardingStep.PROFILE: OnboardingStep.CONSENT,
            OnboardingStep.CONNECT: OnboardingStep.PROFILE,
        }
        prev = back_map.get(self.state.current_step)
        if prev:
            self.state.current_step = prev
            self._save_state()
        return self.get_current_step()

    def is_complete(self) -> bool:
        """Check if onboarding is complete."""
        return self.state.current_step == OnboardingStep.DONE

    def reset(self):
        """Reset onboarding to the beginning."""
        self.state = OnboardingState()
        self._save_state()


# =============================================================================
# Setup doctor — portabilidad a otra PC (sin requerir admin)
# =============================================================================
# Todo lo de esta seccion es lectura + `ollama pull` (nivel usuario).
# Docker NO se instala a ciegas: requiere admin/WSL2+reboot, asi que si falta
# solo se genera install-docker.md con pasos y el check queda manual-pending.

import os as _os
import platform as _platform
import shutil as _shutil
import socket as _socket
import subprocess as _subprocess
import urllib.request as _urllib_request
from typing import Callable as _Callable

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OLLAMA_TAGS_PATH = "/api/tags"
DOCKER_WINGET_CMD = "winget install -e --id Docker.DockerDesktop"
DOCKER_GUIDE_NAME = "install-docker.md"

# Plan basico ampliable: la clave es el perfil detectado.
# - light: sin GPU o poca RAM -> chat general + embeddings
# - standard: GPU media -> light + codigo
# - workstation: VRAM amplia -> standard + vision
# - cpu: sin GPU detectada -> liviano + embeddings
MODEL_PROFILES: dict[str, list[str]] = {
    "cpu": ["nemotron-3-nano:4b", "nomic-embed-text"],
    "light": ["qwen3.5-9b", "nomic-embed-text"],
    "standard": ["qwen3.5-9b", "nomic-embed-text", "qwen2.5-coder:7b"],
    "workstation": ["qwen3.5-9b", "nomic-embed-text", "qwen2.5-coder:7b", "qwen3-vl-8b"],
}

_MODEL_REASONS: dict[str, str] = {
    "nemotron-3-nano:4b": "chat liviano para CPU sin GPU",
    "qwen3.5-9b": "chat general + tool-calling",
    "nomic-embed-text": "embeddings para RAG",
    "qwen2.5-coder:7b": "codigo especializado",
    "qwen3-vl-8b": "vision principal",
}

_CHECK_OK = "ok"
_CHECK_MISSING = "missing"
_CHECK_ERROR = "error"
_CHECK_MANUAL = "manual-pending"
_CHECK_UNKNOWN = "unknown"


def _make_check(name: str, status: str, detail: str = "", fix: str = "") -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def ollama_base_url() -> str:
    """Base URL de Ollama (sobreescribible con OLLAMA_HOST, sin hardcodear)."""
    return (_os.environ.get("OLLAMA_HOST") or OLLAMA_DEFAULT_URL).rstrip("/")


def detect_hardware() -> dict:
    """CPU/RAM/disco via stdlib (+psutil si esta) y GPU/VRAM via nvidia-smi.

    Nunca requiere admin. Si algo no se puede saber, queda "unknown".
    """
    cpu_count = _os.cpu_count() or 0
    cpu_name = _platform.processor() or _platform.machine() or "unknown"

    ram_gb: float | str = "unknown"
    try:
        import psutil  # type: ignore

        ram_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        # Fallback Windows sin psutil: GlobalMemoryStatusEx via ctypes.
        try:
            import ctypes  # type: ignore

            class _MemStat(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStat()
            stat.dwLength = ctypes.sizeof(_MemStat)
            _windll = getattr(ctypes, "windll", None)
            if _windll is not None and _windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                ram_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            pass

    try:
        _total, _used, free = _shutil.disk_usage(str(Path.cwd().anchor))
        disk_free_gb: float | str = round(free / (1024 ** 3), 1)
    except Exception:
        disk_free_gb = "unknown"

    gpu_name: str = "unknown"
    vram_gb: float | str = "unknown"
    try:
        from src.core.hardware import detect_vram_gb  # reutiliza nvidia-smi

        vram = detect_vram_gb()
        if vram is not None:
            vram_gb = vram
            gpu_name = "nvidia"
    except Exception:
        pass
    if vram_gb == "unknown" and _shutil.which("nvidia-smi") is None:
        gpu_name = "none-detected"

    return {
        "cpu_count": cpu_count,
        "cpu_name": cpu_name,
        "ram_gb": ram_gb,
        "disk_free_gb": disk_free_gb,
        "gpu_name": gpu_name,
        "vram_gb": vram_gb,
    }


def select_profile(hw: dict) -> str:
    """Perfil de modelos segun hardware detectado."""
    vram = hw.get("vram_gb")
    ram = hw.get("ram_gb")
    ram_f = ram if isinstance(ram, (int, float)) else 0.0
    if isinstance(vram, (int, float)):
        if vram >= 12:
            return "workstation"
        return "standard"
    # Sin VRAM detectable: distingue CPU-only de light por RAM.
    if hw.get("gpu_name") == "none-detected" and ram_f and ram_f < 8:
        return "light"
    if hw.get("gpu_name") == "none-detected":
        # Sin GPU: liviano CPU, salvo RAM holgada que aguanta el general.
        return "cpu" if (not ram_f or ram_f < 16) else "light"
    return "light"


def check_ollama(base_url: str = "", timeout_s: float = 5.0) -> dict:
    """GET {base}/api/tags sin admin. Lista modelos instalados."""
    base = (base_url or ollama_base_url()).rstrip("/")
    url = base + OLLAMA_TAGS_PATH
    try:
        req = _urllib_request.Request(url, method="GET")
        with _urllib_request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as e:
        return _make_check(
            "ollama",
            _CHECK_MISSING,
            f"no responde en {url}: {e}",
            "instala Ollama desde https://ollama.com/download e inícialo (ollama serve)",
        )
    try:
        models = [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]
    except Exception:
        models = []
    return _make_check(
        "ollama",
        _CHECK_OK,
        f"responde en {base} con {len(models)} modelo(s): {', '.join(models) or 'ninguno'}",
        "",
    )


def _ollama_model_names(check: dict) -> list[str]:
    detail = check.get("detail", "")
    after = detail.split("modelo(s):", 1)
    if len(after) < 2:
        return []
    tail = after[1].strip()
    if tail == "ninguno":
        return []
    return [m.strip() for m in tail.split(",") if m.strip()]


def check_hardware() -> dict:
    hw = detect_hardware()
    return _make_check(
        "hardware",
        _CHECK_OK,
        (
            f"cpu={hw['cpu_name']}x{hw['cpu_count']} "
            f"ram={hw['ram_gb']}GB vram={hw['vram_gb']} "
            f"gpu={hw['gpu_name']} disco_libre={hw['disk_free_gb']}GB"
        ),
        "",
    )


def check_docker(timeout_s: float = 15.0) -> dict:
    """Distingue ausente (manual-pending + guia) vs sin daemon (error con fix)."""
    if _shutil.which("docker") is None:
        return _make_check(
            "docker",
            _CHECK_MANUAL,
            "cliente docker no encontrado en PATH",
            f"requiere admin/WSL2+reboot: sigue {DOCKER_GUIDE_NAME} ({DOCKER_WINGET_CMD})",
        )
    try:
        proc = _subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except Exception as e:
        return _make_check(
            "docker",
            _CHECK_ERROR,
            f"no se pudo ejecutar docker info: {e}",
            "reinstala Docker Desktop o revisa el PATH",
        )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:300]
        return _make_check(
            "docker",
            _CHECK_ERROR,
            f"docker sin daemon: {err or 'docker info fallo'}",
            "inicia Docker Desktop y reintenta",
        )
    return _make_check("docker", _CHECK_OK, "docker info responde", "")


def check_network(timeout_s: float = 3.0) -> dict:
    """Opcional: nunca tumba el ok global si falla (queda unknown)."""
    try:
        _socket.create_connection(("8.8.8.8", 53), timeout=timeout_s).close()
        return _make_check("red", _CHECK_OK, "salida a internet disponible", "")
    except Exception as e:
        return _make_check("red", _CHECK_UNKNOWN, f"sin verificar salida: {e}", "")


def run_doctor() -> dict:
    """Auditoria completa sin admin. ok=True solo si todo esta ok o unknown-red."""
    hw = detect_hardware()
    hw_check = _make_check(
        "hardware",
        _CHECK_OK,
        (
            f"cpu={hw.get('cpu_name', '?')}x{hw.get('cpu_count', '?')} "
            f"ram={hw.get('ram_gb', '?')}GB vram={hw.get('vram_gb', '?')} "
            f"gpu={hw.get('gpu_name', '?')} disco_libre={hw.get('disk_free_gb', '?')}GB"
        ),
        "",
    )
    ollama_check = check_ollama()
    docker_check = check_docker()
    net_check = check_network()
    checks = [hw_check, ollama_check, docker_check, net_check]
    ok = all(c["status"] in (_CHECK_OK, _CHECK_UNKNOWN) for c in checks)
    profile = select_profile(hw)
    return {
        "ok": ok,
        "profile": profile,
        "hardware": hw,
        "ollama_models": _ollama_model_names(ollama_check),
        "checks": checks,
    }


def plan_models(hw: dict, instalados: list[str]) -> list[dict[str, str]]:
    """Dados hw e instalados, devuelve pulls pendientes con comando exacto."""
    profile = select_profile(hw)
    wanted = MODEL_PROFILES[profile]
    have = {m.split(":")[0].lower() for m in instalados}

    def _have(name: str) -> bool:
        base = name.split(":")[0].lower()
        return name.lower() in {m.lower() for m in instalados} or base in have

    pending: list[dict[str, str]] = []
    for name in wanted:
        if not _have(name):
            pending.append(
                {
                    "name": name,
                    "pull_cmd": f"ollama pull {name}",
                    "reason": _MODEL_REASONS.get(name, f"requerido por perfil {profile}"),
                }
            )
    return pending


def ensure_ollama_models(
    pending: list[dict[str, str]],
    timeout_s: float = 1800.0,
    log: _Callable[[str], None] | None = None,
) -> list[dict[str, str]]:
    """Ejecuta `ollama pull X` por modelo pendiente con progreso/log.

    No requiere admin. Si el binario ollama falta, cada item queda en error
    con el fix de descarga (no se intenta nada a ciegas).
    """
    emit = log or (lambda _m: None)
    if _shutil.which("ollama") is None:
        emit("binario ollama no encontrado en PATH")
        return [
            {
                "name": p["name"],
                "status": _CHECK_ERROR,
                "detail": "binario ollama ausente",
                "fix": "instala Ollama desde https://ollama.com/download",
            }
            for p in pending
        ]
    results: list[dict[str, str]] = []
    for p in pending:
        name = p["name"]
        emit(f"pull {name} ...")
        try:
            proc = _subprocess.run(
                ["ollama", "pull", name],
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()
            tail_s = tail[-1] if tail else ""
            if proc.returncode == 0:
                emit(f"pull {name}: OK {tail_s}")
                results.append({"name": name, "status": _CHECK_OK, "detail": tail_s or "pull ok", "fix": ""})
            else:
                emit(f"pull {name}: FALLO {tail_s}")
                results.append(
                    {
                        "name": name,
                        "status": _CHECK_ERROR,
                        "detail": tail_s or f"ollama pull {name} fallo (rc={proc.returncode})",
                        "fix": f"reintenta manual: ollama pull {name}",
                    }
                )
        except Exception as e:
            emit(f"pull {name}: ERROR {e}")
            results.append({"name": name, "status": _CHECK_ERROR, "detail": str(e)[:300], "fix": f"reintenta manual: ollama pull {name}"})
    return results


def write_docker_guide(project_root: Path | None = None) -> str:
    """Genera install-docker.md (Docker requiere admin: solo guia, no instala)."""
    root = project_root or Path(__file__).resolve().parents[2]
    path = root / DOCKER_GUIDE_NAME
    path.write_text(
        "# Instalar Docker Desktop (requiere admin)\n\n"
        "Nexus v3 detectó que el cliente `docker` falta o el daemon no responde.\n"
        "Docker Desktop necesita privilegios de administrador, WSL2 y un reboot:\n"
        "por eso el setup NO lo instala solo. Pasos manuales:\n\n"
        "1. Abre una terminal **como administrador**.\n"
        f"2. Ejecuta: `{DOCKER_WINGET_CMD}`\n"
        "3. Activa WSL2 si el instalador lo pide (`wsl --install`).\n"
        "4. Reinicia la PC cuando lo pida el instalador.\n"
        "5. Abre Docker Desktop y espera a que el icono quede en verde.\n"
        "6. Verifica con: `docker info` (sin errores = listo).\n"
        "7. Re-ejecuta el setup: `python -m src.api.server setup --check`.\n",
        encoding="utf-8",
    )
    return str(path)


def format_summary(report: dict) -> str:
    lines = [f"setup: {'OK' if report.get('ok') else 'PENDIENTE'}  perfil={report.get('profile')}"]
    for c in report.get("checks", []):
        mark = "OK" if c["status"] == _CHECK_OK else c["status"].upper()
        lines.append(f"  [{mark}] {c['name']}: {c['detail']}")
        if c.get("fix"):
            lines.append(f"        fix: {c['fix']}")
    pending = plan_models(report.get("hardware", {}), report.get("ollama_models", []))
    if pending:
        lines.append("modelos pendientes:")
        for p in pending:
            lines.append(f"  - {p['name']}  ({p['reason']})  ->  {p['pull_cmd']}")
    else:
        lines.append("modelos: completos para el perfil")
    return "\n".join(lines)


def run_setup(fix: bool = False, project_root: Path | None = None) -> int:
    """Entry del CLI setup: audita siempre; con fix=True instala lo instalable."""
    report = run_doctor()
    print(format_summary(report))
    if not fix:
        if not report["ok"]:
            print("\nRe-ejecuta con --fix para instalar modelos, o atiende los fix de arriba.")
        return 0 if report["ok"] else 1
    pending = plan_models(report.get("hardware", {}), report.get("ollama_models", []))
    if pending:
        print("")
        results = ensure_ollama_models(pending, log=print)
        if any(r["status"] != _CHECK_OK for r in results):
            return 1
    for c in report.get("checks", []):
        if c["name"] == "docker" and c["status"] == _CHECK_MANUAL:
            guide = write_docker_guide(project_root)
            print(f"Docker manual-pending: guia generada en {guide}")
            return 1
    report2 = run_doctor()
    print("")
    print(format_summary(report2))
    return 0 if report2["ok"] else 1
