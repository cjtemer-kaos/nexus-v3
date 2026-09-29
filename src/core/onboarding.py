"""
Onboarding for SuperNEXUS v2.

Consent-gated state machine: welcome → consent → profile → connect → done.
Each step requires explicit user confirmation before proceeding.
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
                "title": "Welcome to SuperNEXUS",
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
                "description": "Onboarding complete. SuperNEXUS is ready to use.",
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
