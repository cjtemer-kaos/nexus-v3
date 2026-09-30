from __future__ import annotations
"""SkillOpt — paso 4 del ciclo de autoaprendizaje (ciclo #113, colmena #113/#132).

NUEVO en v3 (en v2 era un stub: self_improving_agent_skill con 0 imports).

Regla central: los cambios de harness (prompts/config) solo se PERSISTEN como
aceptados si validan contra el set held-out. Fail-closed: sin validacion, sin
persistencia. El evaluator es inyectable (sync o async) para poder validar
contra el pipeline real del server o contra mocks en tests.
"""
import inspect
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from src.core.environment import data_home

logger = logging.getLogger(__name__)

# Held-out de arranque: smoke cases deterministas (el held-out real lo define
# el usuario via POST /api/skill-opt/heldout). Se documenta como placeholder.
_SEED_HELDOUT = [
    {"id": "h1", "task": "Responde unicamente con la palabra OK", "expect_contains": "OK"},
    {"id": "h2", "task": "Escribe el numero 42", "expect_contains": "42"},
]


class SkillOptimizer:
    """Gate held-out para cambios de harness.

    Estados de un cambio:
      proposed -> (validate) -> accepted (config_delta persistido en state)
                             -> rejected  (NO se persiste)
    """

    def __init__(self,
                 state_path: Optional[Path] = None,
                 heldout_path: Optional[Path] = None):
        self.state_path = Path(state_path) if state_path else data_home() / "skill_opt_state.json"
        self.heldout_path = Path(heldout_path) if heldout_path else data_home() / "heldout.json"
        self._state: dict[str, Any] = {"baseline_score": 0.0, "changes": {}}
        self._heldout: list[dict] = []
        self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.state_path.exists():
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and "changes" in loaded:
                    self._state = loaded
        except Exception as e:
            logger.warning("SkillOpt state corrupto, arranco limpio: %s", e)
        try:
            if self.heldout_path.exists():
                loaded = json.loads(self.heldout_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    self._heldout = loaded
        except Exception as e:
            logger.warning("SkillOpt held-out corrupto, uso seed: %s", e)
        if not self._heldout:
            self._heldout = list(_SEED_HELDOUT)
            self._save_heldout()

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")

    def _save_heldout(self) -> None:
        self.heldout_path.parent.mkdir(parents=True, exist_ok=True)
        self.heldout_path.write_text(
            json.dumps(self._heldout, indent=2, ensure_ascii=False), encoding="utf-8")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def heldout(self) -> list[dict]:
        return list(self._heldout)

    def set_heldout(self, cases: list[dict]) -> int:
        """Reemplaza el held-out (solo con casos {id?, task, expect_contains})."""
        clean = []
        for c in cases or []:
            if isinstance(c, dict) and c.get("task"):
                clean.append({
                    "id": c.get("id") or f"h{len(clean)+1}",
                    "task": str(c["task"]),
                    "expect_contains": str(c.get("expect_contains", "")),
                })
        if not clean:
            raise ValueError("held-out vacio: se necesitan casos con 'task'")
        self._heldout = clean
        self._save_heldout()
        return len(self._heldout)

    def propose(self, description: str, config_delta: dict | None = None,
                change_id: str | None = None) -> dict:
        cid = change_id or f"chg_{uuid.uuid4().hex[:8]}"
        ch = {
            "change_id": cid,
            "description": str(description),
            "config_delta": config_delta or {},
            "status": "proposed",
            "score": 0.0,
            "created_at": time.time(),
            "validated_at": 0.0,
            "results": [],
        }
        self._state.setdefault("changes", {})[cid] = ch
        self._save()
        return dict(ch)

    async def validate(
        self,
        change_id: str,
        evaluator: Callable[[dict], Any],
        required_score: float = 1.0,
        cases: Optional[list[dict]] = None,
    ) -> dict:
        """Valida un cambio contra held-out. Persiste SOLO si score >= required.

        evaluator: callable(case) -> bool | Awaitable[bool]. Puede ser async
        (pipeline real del server) o sync (tests/mock).
        """
        changes = self._state.setdefault("changes", {})
        if change_id not in changes:
            raise KeyError(f"cambio desconocido: {change_id}")
        ch = changes[change_id]
        test_cases = cases if cases else self.heldout()
        if not test_cases:
            raise ValueError("held-out vacio: nada que validar")

        results = []
        passed = 0
        for case in test_cases:
            try:
                ok = evaluator(case)
                if inspect.isawaitable(ok):
                    ok = await ok
                ok = bool(ok)
            except Exception as e:
                logger.debug("evaluator fallo en caso %s: %s", case.get("id"), e)
                ok = False
            results.append({"id": case.get("id", ""), "passed": ok})
            if ok:
                passed += 1

        score = passed / len(results)
        ch["score"] = round(score, 4)
        ch["validated_at"] = time.time()
        ch["results"] = results
        if score >= required_score:
            ch["status"] = "accepted"
            ch["persisted"] = True  # el unico camino a persistir un cambio
            self._state["baseline_score"] = max(
                float(self._state.get("baseline_score", 0.0)), score)
            logger.info("SkillOpt ACEPTADO %s (score %.2f >= %.2f)",
                        change_id, score, required_score)
        else:
            # fail-closed: el cambio NO se vuelve parte del estado aceptado
            ch["status"] = "rejected"
            ch["persisted"] = False
            logger.info("SkillOpt RECHAZADO %s (score %.2f < %.2f)",
                        change_id, score, required_score)
        self._save()
        return dict(ch)

    def get(self, change_id: str) -> dict | None:
        ch = self._state.get("changes", {}).get(change_id)
        return dict(ch) if ch else None

    def accepted(self) -> list[dict]:
        return [dict(c) for c in self._state.get("changes", {}).values()
                if c.get("status") == "accepted"]

    def stats(self) -> dict:
        changes = self._state.get("changes", {})
        by_status: dict[str, int] = {}
        for c in changes.values():
            s = c.get("status", "?")
            by_status[s] = by_status.get(s, 0) + 1
        return {
            "state_path": str(self.state_path),
            "heldout_cases": len(self._heldout),
            "baseline_score": self._state.get("baseline_score", 0.0),
            "changes_total": len(changes),
            "by_status": by_status,
            "accepted": len(self.accepted()),
        }
