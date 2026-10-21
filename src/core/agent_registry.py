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
_STR_FIELDS = ("name", "model", "provider", "system_prompt", "description", "emoji", "color", "gema", "mode", "template")
_FIELDS = _STR_FIELDS + _LIST_FIELDS
_FIELD_LIMITS: dict[str, int] = {
    "system_prompt": 16000, "description": 500, "name": 60,
    "model": 200, "provider": 100, "emoji": 8, "color": 32, "gema": 64,
    "mode": 16, "template": 32,
}

AGENT_MODES = ("build", "read_only")

# Plantillas de rol inspiradas en los equipos de OpenCode Ensemble:
# explorador read-only, constructor, revisor read-only, QA e investigador.
# La plantilla solo aporta defaults; cualquier campo explicito gana.
AGENT_TEMPLATES: dict[str, dict[str, Any]] = {
    "scout": {
        "description": "Explora el repo y reporta rutas, riesgos y el plan minimo. No edita archivos.",
        "system_prompt": (
            "Eres un scout read-only. Explora el repositorio con read_file, list_dir, glob, "
            "code_search y repo_map. Reporta archivos concretos, flujo de datos, riesgos y un "
            "plan de implementacion minimo. No edites, no ejecutes comandos mutantes y no "
            "inventes rutas: cita evidencia."
        ),
        "gema": "scholar",
        "mode": "read_only",
        "toolsets": ["read", "search"],
    },
    "builder": {
        "description": "Implementa cambios acotados y los verifica con tests. Edita solo lo necesario.",
        "system_prompt": (
            "Eres un builder. Implementa el cambio mas pequeno que resuelva la tarea, respeta "
            "el estilo del repo, agrega o ajusta tests y verifica con el comando real. No "
            "refactorices de mas ni toques secretos. Reporta archivos, diff y evidencia."
        ),
        "gema": "code",
        "mode": "build",
        "toolsets": ["read", "write", "exec", "search"],
    },
    "reviewer": {
        "description": "Revisa un diff o una propuesta y reporta bugs, riesgos y tests faltantes. No edita.",
        "system_prompt": (
            "Eres un reviewer read-only. Revisa el diff, los tests y los contratos afectados. "
            "Prioriza bugs reales, regresiones, seguridad y rendimiento. Clasifica hallazgos "
            "por severidad y cita archivo:linea. No edites archivos."
        ),
        "gema": "security",
        "mode": "read_only",
        "toolsets": ["read", "search"],
    },
    "qa": {
        "description": "Disena y ejecuta pruebas de regresion para el cambio.",
        "system_prompt": (
            "Eres QA. Identifica el comportamiento esperado, agrega pruebas de regresion "
            "reproducibles y ejecuta la suite. Reporta comando, salida y casos no cubiertos. "
            "No cambies codigo de produccion salvo que sea imprescindible para testear."
        ),
        "gema": "tester",
        "mode": "build",
        "toolsets": ["read", "write", "exec", "search"],
    },
    "researcher": {
        "description": "Investiga documentacion, APIs y antecedentes; devuelve evidencia y fuentes.",
        "system_prompt": (
            "Eres un investigador. Consulta documentacion oficial y fuentes primarias, "
            "contrasta versiones y devuelve una sintesis con enlaces, fecha y limitaciones. "
            "No presentes supuestos como hechos."
        ),
        "gema": "scholar",
        "mode": "read_only",
        "toolsets": ["read", "search", "web"],
    },
    "ops": {
        "description": "Automatiza tareas de build, deploy o diagnostico con comandos verificables.",
        "system_prompt": (
            "Eres un operador DevOps. Antes de cambiar algo, inspecciona el estado, hace backup "
            "si corresponde y verifica el resultado con evidencia. No ejecutes acciones "
            "destructivas sin confirmacion explicita."
        ),
        "gema": "devops",
        "mode": "build",
        "toolsets": ["read", "write", "exec", "search"],
    },
}


def list_templates() -> list[dict[str, Any]]:
    """Catalogo de plantillas para UI y para la tool agent_create."""
    return [{"name": name, **{k: v for k, v in tpl.items()}} for name, tpl in AGENT_TEMPLATES.items()]


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
        "mode": "build",
        "template": "",
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
                        a.setdefault("mode", "build")
                        a.setdefault("template", "")
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

    def create(self, *, name: str, template: str = "", **fields: Any) -> dict[str, Any]:
        aid = normalize_agent_id(name)
        if not aid or not _ID_RE.match(aid):
            raise ValueError("nombre de ayudante inválido (usa letras/números/guiones)")
        tpl_name = (template or "").strip().lower()
        if tpl_name and tpl_name not in AGENT_TEMPLATES:
            raise ValueError(f"plantilla desconocida '{tpl_name}' (usa: {', '.join(AGENT_TEMPLATES)})")
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
                "mode": "build",
                "template": tpl_name,
                "workspace": f"{aid}/workspace",
                "toolsets": [],
                "skills": [],
                "mcp": [],
                "created_at": _now(),
                "updated_at": _now(),
                "builtin": False,
            }
            tpl = AGENT_TEMPLATES.get(tpl_name) if tpl_name else None
            if tpl:
                for key in ("description", "system_prompt", "gema", "mode", "toolsets", "skills", "mcp"):
                    if key in tpl:
                        value = tpl[key]
                        agent[key] = list(value) if isinstance(value, list) else value
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
                "mode": (a.get("mode") or "build").strip() or "build",
                "template": (a.get("template") or "").strip(),
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
            elif key == "mode":
                mode = str(value or "").strip().lower()
                if mode and mode not in AGENT_MODES:
                    raise ValueError(f"modo inválido '{mode}' (usa: {', '.join(AGENT_MODES)})")
                if mode:
                    agent[key] = mode
            elif key == "template":
                tpl = str(value or "").strip().lower()
                if tpl and tpl not in AGENT_TEMPLATES:
                    raise ValueError(f"plantilla desconocida '{tpl}' (usa: {', '.join(AGENT_TEMPLATES)})")
                agent[key] = tpl
            else:
                agent[key] = str(value or "").strip()[:_FIELD_LIMITS.get(key, 200)]
