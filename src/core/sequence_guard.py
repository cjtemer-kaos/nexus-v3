"""Guardrail de secuencia (ToolShield).

El actor no decide su propio riesgo: este modulo observa las ultimas acciones y
clasifica la proxima como LOW / MEDIUM / HIGH. La decision final la toma
``ToolRegistry.dispatch`` (HIGH = deny; MEDIUM = aprobacion humana o deny
fail-closed si no hay canal).

Modos:
  - ``heuristic``: solo patrones locales, sin LLM.
  - ``auto``: heuristic; si la secuencia es sospechosa (MEDIUM) consulta un LLM
    barato para confirmar/bajar el riesgo. LOW/HIGH no gastan llamada.
  - ``llm``: consulta el LLM para toda accion que no sea HIGH deterministico.

Si el LLM falla o tarda, se usa el veredicto heuristico (fail-safe, no fail-open
para HIGH).
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("nexus-sequence-guard")

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"

# Patrones catastroficos/irreversibles: HIGH sin consultar al LLM.
_HIGH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\brm\s+-rf\s+(?:/|~|\$home|\$env:userprofile)(?:\s|$)", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r":\(\)\s*\{", re.I),
    re.compile(r"\bformat\s+[a-z]:", re.I),
    re.compile(r"\b(?:shutdown|reboot|diskpart|bcdedit)\b", re.I),
    re.compile(r"\bdel\s+/[fs]\s+/[sq]\s+[a-z]:\\", re.I),
    re.compile(r"\brd\s+/s\s+/q\s+[a-z]:\\", re.I),
    re.compile(r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)(?:\s|$)", re.I),
    re.compile(r"\bgit\s+reset\s+--hard\b", re.I),
    re.compile(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:ba|z|k)?sh\b", re.I),
)

# Lectura de secretos: MEDIUM (puede ser legitima, pero no se auto-aprueba).
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[\s\"'\\/])\.env(?:[\s\"'\\/]|$)", re.I),
    re.compile(r"\.(?:key|pem|pfx|p12)(?:[\s\"']|$)", re.I),
    re.compile(
        r"\b(?:credentials|secrets?|id_rsa|id_ed25519)(?:\.|$|[\s\"'\\/])", re.I),
)

# Herramientas de solo lectura: repetirlas no escala a MEDIUM por si solo
# (el anti-loop del AgentLoop ya cubre el bucle). Si tocan secretos, el patron
# anterior las marca igual.
_READ_ONLY_TOOLS = frozenset({
    "read_file", "read", "list_dir", "glob", "grep", "code_search", "repo_map",
    "web_fetch", "web_search", "recall", "memory_recall", "search_observations",
    "browser_snapshot", "browser_console",
})

_SENSITIVE_COMMAND = re.compile(
    r"\b(?:git\s+clean\b[^\n]*-[a-z]*f|docker\s+(?:system\s+)?prune\b|"
    r"kubectl\s+delete\b|terraform\s+(?:apply|destroy)\b|"
    r"(?:npm|pnpm|yarn)\s+publish\b|twine\s+upload\b|gh\s+release\s+create\b|"
    r"docker\s+push\b|aws\b[^\n]*\bdelete-|gcloud\b[^\n]*\bdelete\b)\b",
    re.I,
)


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    risk: str = RISK_LOW
    reason: str = ""
    source: str = "heuristic"

    @property
    def high(self) -> bool:
        return self.risk == RISK_HIGH

    @property
    def medium(self) -> bool:
        return self.risk == RISK_MEDIUM

    def to_dict(self) -> dict:
        return {"risk": self.risk, "reason": self.reason, "source": self.source}


@dataclass(frozen=True, slots=True)
class PolicyRule:
    """Regla declarativa: gana la de mayor severidad entre las que matchean."""

    id: str
    action: str
    tools: tuple[str, ...]
    pattern: re.Pattern[str] | None
    path_pattern: re.Pattern[str] | None
    reason: str

    def matches(self, tool: str, args: dict) -> bool:
        if not any(fnmatch.fnmatchcase(tool, t) for t in self.tools):
            return False
        text = _command_text(args)
        haystack = text or _scan_haystack(args)
        if self.pattern is not None and not self.pattern.search(haystack):
            return False
        if self.path_pattern is not None:
            if not self.path_pattern.search(_scan_haystack(args)):
                return False
        return True


_POLICY_ACTIONS = {
    "deny": RISK_HIGH,
    "high": RISK_HIGH,
    "medium": RISK_MEDIUM,
    "approve": RISK_MEDIUM,
    "allow": RISK_LOW,
    "low": RISK_LOW,
}
_POLICY_SEVERITY = ("deny", "high", "medium", "approve", "allow", "low")


def _policy_file(path_value: str) -> Path | None:
    path = Path(path_value)
    if path.is_absolute():
        return path if path.exists() else None
    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        candidate = base / path
        if candidate.exists():
            return candidate
    return None


def _load_policy_rules(policy_cfg: dict) -> list[PolicyRule]:
    raw_rules: list[dict] = []
    file_value = policy_cfg.get("file")
    if isinstance(file_value, str) and file_value.strip():
        candidate = _policy_file(file_value.strip())
        if candidate is not None:
            try:
                import yaml

                data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    data = data.get("rules") or []
                if isinstance(data, list):
                    raw_rules.extend(x for x in data if isinstance(x, dict))
            except Exception as e:
                logger.warning("policy de secuencia ilegible (%s): %s", candidate, e)
        else:
            logger.warning("policy de secuencia no encontrada: %s", file_value)
    inline = policy_cfg.get("rules")
    if isinstance(inline, list):
        raw_rules.extend(x for x in inline if isinstance(x, dict))

    rules: list[PolicyRule] = []
    for i, raw in enumerate(raw_rules, 1):
        action = str(raw.get("action") or "").strip().lower()
        if action not in _POLICY_ACTIONS:
            logger.warning("policy rule %s con action invalida: %r", i, action)
            continue
        tools = raw.get("tools", ["*"])
        if isinstance(tools, str):
            tools = [tools]
        tools = tuple(str(t) for t in tools if str(t).strip()) or ("*",)
        flags = 0 if raw.get("case_sensitive") else re.I
        try:
            pattern = re.compile(str(raw["pattern"]), flags) if raw.get("pattern") else None
            path_pattern = (re.compile(str(raw["path_pattern"]), flags)
                            if raw.get("path_pattern") else None)
        except re.error as e:
            logger.warning("policy rule %s con regex invalida: %s", i, e)
            continue
        rules.append(PolicyRule(
            id=str(raw.get("id") or f"rule-{i}"),
            action=action,
            tools=tools,
            pattern=pattern,
            path_pattern=path_pattern,
            reason=str(raw.get("reason") or f"policy:{raw.get('id') or i}")[:300],
        ))
    return rules


def _fingerprint(tool: str, args: dict | None) -> str:
    raw = json.dumps({"tool": tool, "args": args or {}}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _command_text(args: dict | None) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("command", "cmd", "shell", "script"):
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, list):
            return " ".join(str(x) for x in v)
    return ""


# Lo que un archivo DICE no es lo que la accion HACE. Un backend con
# `load_dotenv()` y `SECRET_KEY` en el codigo menciona ".env" y "secret": el
# guard miraba `json.dumps(args)` entero, marcaba MEDIUM "toca secretos" y sin
# canal interactivo (gemas, sub-agentes) el write_file quedaba bloqueado para
# siempre. La ruta (`path`) SI se sigue mirando: escribir en `.env` es MEDIUM.
_BODY_KEYS = frozenset({
    "content", "new", "old", "patch", "diff", "text", "body", "value", "data",
    "preview", "code",
})

_PATCH_FILE = re.compile(
    r"^\*\*\*\s*(?:Update|Add|Delete) File:\s*(.+)$|^\*\*\*\s*Move to:\s*(.+)$",
    re.M,
)


def _scan_haystack(args: dict | None) -> str:
    """Args sin el cuerpo del archivo: rutas, comandos y nombres, nada mas."""
    if not isinstance(args, dict):
        return str(args or "")
    limpio = {k: v for k, v in args.items() if k not in _BODY_KEYS}
    parche = args.get("patch")
    if isinstance(parche, str) and parche:
        # De un parche solo interesan las rutas que toca, no el codigo.
        limpio["patch_archivos"] = [
            (a or b) for a, b in _PATCH_FILE.findall(parche)
        ][:50]
    try:
        return json.dumps(limpio, sort_keys=True, default=str)
    except Exception:
        return str(limpio)


def _preview(tool: str, args: dict | None, limit: int = 240) -> str:
    text = _command_text(args)
    if text:
        return text[:limit]
    try:
        return _scan_haystack(args)[:limit]
    except Exception:
        return str(args)[:limit]


class SequenceGuard:
    """Ring buffer de acciones + clasificador de riesgo."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        router=None,
        session_id: str = "",
        event_store: Any = None,
    ):
        cfg = ((config or {}).get("guard") or {}).get("sequence") or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.mode = str(cfg.get("mode", "auto") or "auto").lower()
        if self.mode not in ("heuristic", "auto", "llm"):
            self.mode = "auto"
        self.window = max(1, min(200, int(cfg.get("window", 20) or 20)))
        self.repeat_medium = max(2, int(cfg.get("repeat_medium", 3) or 3))
        self.failure_streak_medium = max(
            1, int(cfg.get("failure_streak_medium", 2) or 2))
        self.timeout_s = max(0.1, float(cfg.get("timeout_s", 2.0) or 2.0))
        self.model_hint = str(cfg.get("model_hint", "simple") or "simple")
        policy_cfg = cfg.get("policy") or {}
        self.policy_enabled = bool(policy_cfg.get("enabled", True)) if policy_cfg else False
        self.policy_rules = _load_policy_rules(policy_cfg) if self.policy_enabled else []
        self.router = router
        self.session_id = session_id
        self.event_store = event_store
        self._history: deque[dict] = deque(maxlen=self.window)
        self._cache: dict[str, RiskVerdict] = {}
        self._seeded = False

    # ------------------------------------------------------------------
    # Historial
    # ------------------------------------------------------------------
    def _seed_from_events(self) -> None:
        if self._seeded or self.event_store is None:
            return
        self._seeded = True
        try:
            events = self.event_store.get_events(
                session_id=self.session_id or None, kind="tool_call", limit=1000)
            for ev in events[-self.window:]:
                data = getattr(ev, "data", {}) or {}
                self._history.append({
                    "tool": str(data.get("tool") or ""),
                    "args": data.get("args") or {},
                    "success": bool(data.get("success")),
                    "outcome": str(data.get("outcome") or ""),
                })
        except Exception as e:
            logger.debug("no se pudo sembrar historial de secuencia: %s", e)

    def record(self, tool: str, args: dict | None, success: bool,
               outcome: str = "", error: str = "") -> None:
        if not self.enabled:
            return
        self._history.append({
            "tool": str(tool or ""),
            "args": args or {},
            "success": bool(success),
            "outcome": str(outcome or ("ok" if success else "error")),
            "error": str(error or "")[:240],
        })

    def history(self) -> list[dict]:
        self._seed_from_events()
        return list(self._history)

    # ------------------------------------------------------------------
    # Evaluacion
    # ------------------------------------------------------------------
    async def evaluate(self, tool: str, args: dict | None = None) -> RiskVerdict:
        if not self.enabled:
            return RiskVerdict(RISK_LOW, "guard deshabilitado", "disabled")
        self._seed_from_events()
        policy = self._policy_verdict(tool, args or {})
        if policy is not None and policy.risk != RISK_LOW:
            return policy
        heuristic = self._heuristic(tool, args or {})
        if policy is not None:
            if heuristic.risk == RISK_HIGH or "secretos" in heuristic.reason:
                return heuristic
            return policy
        if heuristic.risk == RISK_HIGH:
            return heuristic
        if self.mode == "heuristic" or self.router is None:
            return heuristic
        if self.mode == "auto" and heuristic.risk != RISK_MEDIUM:
            return heuristic
        cache_key = _fingerprint(tool, args or {}) + ":" + hashlib.sha1(
            json.dumps([_fingerprint(h["tool"], h["args"]) for h in self._history],
                       sort_keys=True).encode()).hexdigest()[:12]
        if cache_key in self._cache:
            return self._cache[cache_key]
        verdict = await self._llm_verdict(tool, args or {}, heuristic)
        if len(self._cache) > 128:
            self._cache.clear()
        self._cache[cache_key] = verdict
        return verdict

    def _policy_verdict(self, tool: str, args: dict) -> RiskVerdict | None:
        if not self.policy_enabled or not self.policy_rules:
            return None
        matched = [r for r in self.policy_rules if r.matches(tool, args)]
        if not matched:
            return None
        by_action = {r.action: r for r in matched}
        for action in _POLICY_SEVERITY:
            rule = by_action.get(action)
            if rule is not None:
                return RiskVerdict(
                    _POLICY_ACTIONS[action], rule.reason, f"policy:{rule.id}")
        return None

    def _heuristic(self, tool: str, args: dict) -> RiskVerdict:
        text = _command_text(args)
        # Solo lo que la accion TOCA (rutas/argumentos), nunca el cuerpo del
        # archivo: ver _BODY_KEYS.
        raw_args = _scan_haystack(args)
        for pat in _HIGH_PATTERNS:
            if pat.search(text):
                return RiskVerdict(
                    RISK_HIGH, f"patron catastrofico: {pat.pattern}", "heuristic")
        for pat in _SECRET_PATTERNS:
            if pat.search(text) or pat.search(raw_args):
                return RiskVerdict(
                    RISK_MEDIUM, "la accion toca secretos/credenciales", "heuristic")
        if _SENSITIVE_COMMAND.search(text):
            return RiskVerdict(
                RISK_MEDIUM, "comando sensible o irreversible", "heuristic")
        if tool in _READ_ONLY_TOOLS and not text:
            return RiskVerdict(RISK_LOW, "lectura/verificacion", "heuristic")

        fp = _fingerprint(tool, args)
        same = sum(1 for h in self._history
                   if _fingerprint(h["tool"], h["args"]) == fp)
        if same + 1 >= self.repeat_medium:
            return RiskVerdict(
                RISK_MEDIUM,
                f"la misma accion se repitio {same + 1} veces", "heuristic")

        streak = 0
        for h in reversed(self._history):
            if h.get("success"):
                break
            streak += 1
        if streak >= self.failure_streak_medium:
            return RiskVerdict(
                RISK_MEDIUM,
                f"{streak} fallos consecutivos antes de esta accion", "heuristic")
        return RiskVerdict(RISK_LOW, "secuencia normal", "heuristic")

    async def _llm_verdict(self, tool: str, args: dict,
                           fallback: RiskVerdict) -> RiskVerdict:
        try:
            from .provider_base import LLMMessage
            routed = self.router.resolve(task_hint=self.model_hint)
            if routed is None:
                return fallback
            hist_lines = []
            for i, h in enumerate(self.history()[-self.window:], 1):
                status = "ok" if h.get("success") else "error"
                hist_lines.append(
                    f"{i}. {h.get('tool')}({_preview(h.get('tool',''), h.get('args') or {}, 120)}) -> {status}")
            prompt = (
                "Sos un auditor de seguridad de un agente de codigo. Evalua la ACCION "
                "PROPUESTA en el contexto del historial. Responde SOLO JSON:\n"
                '{"risk":"LOW|MEDIUM|HIGH","reason":"breve"}\n'
                "HIGH solo si es catastrofico/irreversible o exfiltra secretos. "
                "MEDIUM si muta estado sin relacion con el objetivo, repite un patron "
                "fallido o toca credenciales. LOW si es lectura/verificacion normal.\n\n"
                "Historial:\n" + ("\n".join(hist_lines) or "(vacio)") +
                f"\n\nAccion propuesta: {tool}({_preview(tool, args, 400)})"
            )
            resp = await asyncio.wait_for(
                routed.provider.chat(
                    [LLMMessage(role="user", content=prompt)], model=routed.model),
                timeout=self.timeout_s,
            )
            raw = (getattr(resp, "content", "") or "").strip()
            m = re.search(r"\{.*\}", raw, re.S)
            if not m:
                return fallback
            obj = json.loads(m.group(0))
            risk = str(obj.get("risk") or "").upper()
            if risk not in (RISK_LOW, RISK_MEDIUM, RISK_HIGH):
                return fallback
            # El LLM nunca puede bajar un HIGH heuristico (ya retornado antes).
            return RiskVerdict(risk, str(obj.get("reason") or "")[:300], "llm")
        except Exception as e:
            logger.debug("sequence guard LLM fallo: %s", e)
            return fallback
