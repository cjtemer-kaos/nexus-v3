"""Agent registry — ayudantes con LLM independiente (per-assistant config).

Cada ayudante es una entrada persistida en data/agents/registry.json con su
propio model/provider/system_prompt. Nada hardcodeado; el arnés lo resuelve por turno.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_LIST_FIELDS = ("toolsets", "skills", "mcp")
_STR_FIELDS = ("name", "model", "provider", "system_prompt", "description", "emoji", "color", "gema")
_FIELDS = _STR_FIELDS + _LIST_FIELDS
_FIELD_LIMITS: dict[str, int] = {
    "system_prompt": 16000, "description": 500, "name": 60,
    "model": 200, "provider": 100, "emoji": 8, "color": 32, "gema": 64,
}


def normalize_agent_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
    return re.sub(r"^-+|-+$", "", slug)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def default_agent() -> dict[str, Any]:
    return {
        "id": "default",
        "name": "Nexus",
        "model": "",
        "provider": "",
        "system_prompt": "",
        "description": "Ayudante por defecto del arnés (el router elige el modelo).",
        "emoji": "🛰️",
        "color": "#4C9DFF",
        "gema": "",
        "workspace": "default/workspace",
        "toolsets": [],
        "skills": [],
        "mcp": [],
        "created_at": _now(),
        "updated_at": _now(),
        "builtin": True,
    }


class AgentRegistry:
    """Registro de ayudantes persistido en JSON. Thread-safe."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._agents: dict[str, dict[str, Any]] = {}
        self._load()
        for aid in list(self._agents):
            self._ensure_workspace(aid)

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def _ensure_workspace(self, agent_id: str) -> str:
        rel = f"{agent_id}/workspace"
        agent = self._agents.get(agent_id)
        if agent is not None and agent.get("workspace"):
            rel = str(agent["workspace"])
        try:
            (self.base_dir / rel).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if agent is not None and not agent.get("workspace"):
            agent["workspace"] = rel
        return rel

    def _load(self) -> None:
        self._agents = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                for a in data.get("agents", []):
                    if isinstance(a, dict) and a.get("id"):
                        self._agents[str(a["id"])] = a
            except Exception:
                pass
        if "default" not in self._agents:
            self._agents["default"] = default_agent()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "agents": list(self._agents.values())}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(a) for a in self._agents.values()]

    def get(self, agent_id: str) -> dict[str, Any] | None:
        with self._lock:
            a = self._agents.get(agent_id)
            return dict(a) if a else None

    def create(self, *, name: str, **fields: Any) -> dict[str, Any]:
        aid = normalize_agent_id(name)
        if not aid or not _ID_RE.match(aid):
            raise ValueError("nombre de ayudante inválido (usa letras/números/guiones)")
        with self._lock:
            if aid in self._agents:
                raise ValueError(f"ya existe un ayudante '{aid}'")
            agent: dict[str, Any] = {
                "id": aid,
                "name": (name or aid).strip()[:60],
                "model": "",
                "provider": "",
                "system_prompt": "",
                "description": "",
                "emoji": "🤖",
                "color": "#0B62D6",
                "gema": "",
                "workspace": f"{aid}/workspace",
                "toolsets": [],
                "skills": [],
                "mcp": [],
                "created_at": _now(),
                "updated_at": _now(),
                "builtin": False,
            }
            self._apply(agent, fields)
            self._agents[aid] = agent
            self._ensure_workspace(aid)
            self._save()
            return dict(agent)

    def update(self, agent_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            self._apply(agent, patch or {})
            agent["updated_at"] = _now()
            self._save()
            return dict(agent)

    def delete(self, agent_id: str) -> bool:
        with self._lock:
            agent = self._agents.get(agent_id)
            if agent is None:
                raise KeyError(agent_id)
            if agent.get("builtin"):
                raise ValueError("el ayudante por defecto no se puede borrar")
            self._agents.pop(agent_id, None)
            self._save()
            return True

    def resolve(self, agent_id: str | None) -> dict[str, Any]:
        with self._lock:
            a = self._agents.get(agent_id or "default") or self._agents.get("default") or {}
            aid = a.get("id", "default")
            ws_rel = a.get("workspace") or f"{aid}/workspace"
            try:
                ws_abs = str((self.base_dir / ws_rel).resolve())
            except Exception:
                ws_abs = str(self.base_dir / ws_rel)
            return {
                "id": aid,
                "name": a.get("name", "Nexus"),
                "model": (a.get("model") or "").strip(),
                "provider": (a.get("provider") or "").strip(),
                "system_prompt": (a.get("system_prompt") or "").strip(),
                "gema": (a.get("gema") or "").strip(),
                "workspace": ws_rel,
                "workspace_abs": ws_abs,
            }

    @staticmethod
    def _apply(agent: dict[str, Any], patch: dict[str, Any]) -> None:
        for key in _FIELDS:
            if key not in patch:
                continue
            value = patch[key]
            if key in _LIST_FIELDS:
                if isinstance(value, str):
                    value = [s.strip() for s in value.split(",") if s.strip()]
                agent[key] = [str(x).strip() for x in (value or []) if str(x).strip()]
            else:
                agent[key] = str(value or "").strip()[:_FIELD_LIMITS.get(key, 200)]
