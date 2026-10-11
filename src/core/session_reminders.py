"""Session-local reminders — S3-fase1.

Tipos: after / at / every. Session-local: NO toca el cron global de
``src/core/scheduler.py``. La unica delegacion permitida es opt-in via
:meth:`SessionReminderStore.export_to_scheduler_payloads` (genera dicts que
el caller puede pasar a ``TaskScheduler.add_task``; nunca se llama solo).

Reglas (paridad upstream packages/schedule):
- ids ``schedule-N`` monotonos, sin reuso (ni tras cancel/done/restart).
- ``every >= 300s`` (validador estricto).
- fork no hereda (el hijo arranca vacio).
- barrera ``flush()``: persistencia atomica (tmp + rename).
- recurrence latest-only: un ``every`` atrasado dispara UNA vez y salta
  el backlog (``next_fire = now + interval``).
- estados: ``scheduled`` (next_fire > now) / ``overdue`` (next_fire <= now).
- fold de eventos: :meth:`poll` colapsa multiples vencimientos pendientes
  en un unico evento por reminder.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Literal, Optional

from src.core.environment import data_home

MIN_EVERY_SECONDS = 300
ID_PREFIX = "schedule-"

ReminderKind = Literal["after", "at", "every"]
ReminderStatus = Literal["scheduled", "overdue"]


class ReminderError(ValueError):
    """Validador estricto de reminders."""


@dataclass
class Reminder:
    id: str
    session_id: str
    kind: ReminderKind
    message: str
    created_at: float
    next_fire: float
    interval_s: Optional[float] = None  # solo every
    fired_count: int = 0
    done: bool = False
    cancelled: bool = False
    meta: Dict = field(default_factory=dict)

    def status(self, now: Optional[float] = None) -> ReminderStatus:
        now = time.time() if now is None else now
        if self.done or self.cancelled:
            return "scheduled"  # terminales no computan como overdue
        return "overdue" if self.next_fire <= now else "scheduled"

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "session_id": self.session_id, "kind": self.kind,
            "message": self.message, "created_at": self.created_at,
            "next_fire": self.next_fire, "interval_s": self.interval_s,
            "fired_count": self.fired_count, "done": self.done,
            "cancelled": self.cancelled, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "Reminder":
        return cls(
            id=d["id"], session_id=d.get("session_id", ""),
            kind=d["kind"], message=d.get("message", ""),
            created_at=d.get("created_at", 0.0), next_fire=d.get("next_fire", 0.0),
            interval_s=d.get("interval_s"), fired_count=d.get("fired_count", 0),
            done=d.get("done", False), cancelled=d.get("cancelled", False),
            meta=d.get("meta", {}),
        )


def _default_path(session_id: str) -> Path:
    return data_home() / "session_reminders" / f"{session_id}.json"


class SessionReminderStore:
    """Store session-local de reminders con persistencia JSON + flush atomico.

    ``clock`` es inyectable (tests deterministas). ``path=None`` usa
    ``data/session_reminders/<session_id>.json``.
    """

    def __init__(
        self,
        session_id: str,
        path: Optional[str | Path] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        if not session_id or not str(session_id).strip():
            raise ReminderError("session_id requerido")
        self.session_id = session_id
        self.path = Path(path) if path else _default_path(session_id)
        self._clock: Callable[[], float] = clock or time.time
        self._reminders: Dict[str, Reminder] = {}
        self._used_ids: set[str] = set()
        self._counter: int = 0
        self._load()

    # -- persistencia -------------------------------------------------
    def _load(self) -> None:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                if d.get("session_id") not in (None, self.session_id):
                    raise ReminderError("path pertenece a otra sesion")
                self._counter = int(d.get("counter", 0))
                self._used_ids = set(d.get("used_ids", []))
                for rd in d.get("reminders", {}).values():
                    r = Reminder.from_dict(rd)
                    self._reminders[r.id] = r
                    self._used_ids.add(r.id)
        except ReminderError:
            raise
        except Exception:
            self._reminders, self._used_ids, self._counter = {}, set(), 0

    def _snapshot(self) -> Dict:
        return {
            "session_id": self.session_id, "counter": self._counter,
            "used_ids": sorted(self._used_ids),
            "reminders": {k: v.to_dict() for k, v in self._reminders.items()},
        }

    def flush(self) -> None:
        """Barrera: persiste todo atomicamente (tmp + os.replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".rem-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._snapshot(), f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _save(self) -> None:
        self.flush()

    # -- ids -----------------------------------------------------------
    def _next_id(self) -> str:
        self._counter += 1
        rid = f"{ID_PREFIX}{self._counter}"
        while rid in self._used_ids:  # defensa ante counter corrupto
            self._counter += 1
            rid = f"{ID_PREFIX}{self._counter}"
        self._used_ids.add(rid)
        return rid

    # -- validadores ---------------------------------------------------
    @staticmethod
    def _check_message(message: str) -> str:
        if not message or not str(message).strip():
            raise ReminderError("message requerido")
        return str(message)

    @staticmethod
    def _check_delay(delay_s: float) -> float:
        if not isinstance(delay_s, (int, float)) or not math.isfinite(delay_s) or delay_s <= 0:
            raise ReminderError("after requiere delay_s > 0 finito")
        return float(delay_s)

    @staticmethod
    def _check_at(ts: float) -> float:
        if not isinstance(ts, (int, float)) or not math.isfinite(ts):
            raise ReminderError("at requiere timestamp finito")
        return float(ts)

    @staticmethod
    def _check_every(interval_s: float) -> float:
        if not isinstance(interval_s, (int, float)) or not math.isfinite(interval_s):
            raise ReminderError("every requiere interval_s numerico finito")
        if interval_s < MIN_EVERY_SECONDS:
            raise ReminderError(f"every minimo {MIN_EVERY_SECONDS}s (recibido {interval_s})")
        return float(interval_s)

    # -- alta -----------------------------------------------------------
    def add_after(self, delay_s: float, message: str, meta: Optional[Dict] = None) -> Reminder:
        delay = self._check_delay(delay_s)
        msg = self._check_message(message)
        now = self._clock()
        r = Reminder(id=self._next_id(), session_id=self.session_id, kind="after",
                     message=msg, created_at=now, next_fire=now + delay, meta=meta or {})
        self._reminders[r.id] = r
        self._save()
        return r

    def add_at(self, timestamp: float, message: str, meta: Optional[Dict] = None) -> Reminder:
        ts = self._check_at(timestamp)
        msg = self._check_message(message)
        r = Reminder(id=self._next_id(), session_id=self.session_id, kind="at",
                     message=msg, created_at=self._clock(), next_fire=ts, meta=meta or {})
        self._reminders[r.id] = r
        self._save()
        return r

    def add_every(self, interval_s: float, message: str, meta: Optional[Dict] = None) -> Reminder:
        iv = self._check_every(interval_s)
        msg = self._check_message(message)
        now = self._clock()
        r = Reminder(id=self._next_id(), session_id=self.session_id, kind="every",
                     message=msg, created_at=now, next_fire=now + iv,
                     interval_s=iv, meta=meta or {})
        self._reminders[r.id] = r
        self._save()
        return r

    # -- lectura / estado ----------------------------------------------
    def get(self, reminder_id: str) -> Optional[Reminder]:
        return self._reminders.get(reminder_id)

    def list(self, active_only: bool = True) -> List[Reminder]:
        rs = list(self._reminders.values())
        if active_only:
            rs = [r for r in rs if not r.done and not r.cancelled]
        return sorted(rs, key=lambda r: r.next_fire)

    def statuses(self, now: Optional[float] = None) -> Dict[str, ReminderStatus]:
        now = self._clock() if now is None else now
        return {r.id: r.status(now) for r in self._reminders.values()
                if not r.done and not r.cancelled}

    def cancel(self, reminder_id: str) -> bool:
        r = self._reminders.get(reminder_id)
        if not r or r.done or r.cancelled:
            return False
        r.cancelled = True
        self._save()  # el id queda en _used_ids: NO se reusa
        return True

    # -- disparo con fold + latest-only ---------------------------------
    def poll(self, now: Optional[float] = None) -> List[Reminder]:
        """Retorna vencidos (fold: un evento por reminder) y avanza estado.

        - after/at vencido -> se marca done (dispara una sola vez).
        - every vencido -> dispara UNA vez aunque el backlog cubra N
          periodos y salta a ``next_fire = now + interval`` (latest-only).
        Persiste (barrera flush) si hubo cambios.
        """
        now = self._clock() if now is None else now
        fired: List[Reminder] = []
        changed = False
        for r in self._reminders.values():
            if r.done or r.cancelled or r.next_fire > now:
                continue
            fired.append(r)  # fold: un evento por reminder, sin duplicar
            r.fired_count += 1
            changed = True
            if r.kind == "every":
                assert r.interval_s is not None
                r.next_fire = now + r.interval_s  # latest-only: salta backlog
            else:
                r.done = True
        if changed:
            self._save()
        return fired

    # -- fork: NO hereda ---------------------------------------------------
    def fork(self, new_session_id: str, path: Optional[str | Path] = None) -> "SessionReminderStore":
        """Nueva store vacia para la sesion hija (los reminders NO se heredan)."""
        child = SessionReminderStore(session_id=new_session_id,
                                     path=path or _default_path(new_session_id),
                                     clock=self._clock)
        # Si el path hijo ya existia con datos, se respeta (replay), pero
        # nunca se copian los reminders del padre.
        return child

    # -- delegacion opt-in al scheduler global (documentada, nunca auto) --
    def export_to_scheduler_payloads(self, gema: str = "auto") -> List[Dict]:
        """Genera payloads listos para ``TaskScheduler.add_task`` (OPT-IN).

        No importa ni toca ``scheduler.py``; el caller decide si los envia
        al cron global. Solo exporta reminders ``every`` activos.
        """
        out = []
        for r in self.list(active_only=True):
            if r.kind != "every":
                continue
            out.append({
                "name": f"reminder:{r.id}", "prompt": r.message,
                "schedule_type": "once",
                "scheduled_time": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(r.next_fire)),
                "gema": gema, "metadata": {
                    "trigger": "session-reminder", "reminder_id": r.id,
                    "session_id": self.session_id, "every_s": r.interval_s,
                },
            })
        return out
