"""Jobs en background: procesos largos sin bloquear el turno del agente.

El `exec` del harness es sincrónico (timeout 60 s): un build, un `npm run dev`
o una suite de tests mataban la iteración. Acá se lanza el proceso, se guarda su
salida en un log y el agente la consulta por delta (cursor), como un terminal.

Uso:
    job = get_jobs().start(command, cwd, label="pytest")
    get_jobs().read(job["id"])            # todo hasta ahora
    get_jobs().read(job["id"], cursor=… ) # solo lo nuevo
    get_jobs().kill(job["id"])
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


def _jobs_root() -> Path:
    try:
        from .environment import data_home
        return data_home() / "jobs"
    except Exception:
        return Path(__file__).parents[2] / "data" / "jobs"


class JobRegistry:
    """Registro en memoria (por proceso) con logs en disco."""

    def __init__(self, max_jobs: int = 50):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    # ------------------------------------------------------------------
    def start(self, command: str, cwd: Path, label: str = "") -> dict:
        root = _jobs_root()
        root.mkdir(parents=True, exist_ok=True)
        jid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        log = root / f"{jid}.log"
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        env = dict(os.environ)
        env.setdefault("PYTHONUNBUFFERED", "1")
        with open(log, "wb") as fh:
            proc = subprocess.Popen(
                command, shell=True, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=creationflags, env=env,
            )
        rec = {
            "id": jid, "label": label or command[:80], "command": command,
            "cwd": str(cwd), "pid": proc.pid, "log": str(log), "proc": proc,
            "started": time.time(),
        }
        with self._lock:
            self._jobs[jid] = rec
            self._prune_locked()
        return self._public(rec)

    # ------------------------------------------------------------------
    def get(self, job_id: str) -> dict | None:
        with self._lock:
            rec = self._jobs.get(job_id)
        return self._public(rec) if rec else None

    def list(self) -> list[dict]:
        with self._lock:
            recs = list(self._jobs.values())
        return [self._public(r) for r in sorted(recs, key=lambda r: r["started"], reverse=True)]

    def read(self, job_id: str, cursor: int = 0, max_chars: int = 20000) -> dict:
        rec = self._get_rec(job_id)
        log = Path(rec["log"])
        try:
            data = log.read_bytes() if log.exists() else b""
        except Exception:
            data = b""
        start = max(0, int(cursor or 0))
        chunk = data[start:start + max(1, int(max_chars))]
        end = start + len(chunk)
        state = self._state(rec)
        return {
            **self._public(rec),
            "cursor": end,
            "next_cursor": end,
            "pending_bytes": max(0, len(data) - end),
            "output": chunk.decode("utf-8", errors="replace"),
        }

    def wait(self, job_id: str, timeout_s: float = 30.0) -> dict:
        rec = self._get_rec(job_id)
        deadline = time.time() + max(0.0, min(300.0, float(timeout_s)))
        while time.time() < deadline:
            if rec["proc"].poll() is not None:
                break
            time.sleep(0.2)
        return self.read(job_id, cursor=0, max_chars=30000)

    def kill(self, job_id: str) -> dict:
        rec = self._get_rec(job_id)
        proc = rec["proc"]
        if proc.poll() is None:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, timeout=15)
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        return self._public(rec)

    # ------------------------------------------------------------------
    def _get_rec(self, job_id: str) -> dict:
        with self._lock:
            rec = self._jobs.get(job_id)
        if rec is None:
            known = ", ".join(sorted(self._jobs)[-10:]) or "ninguno"
            raise KeyError(f"job desconocido: {job_id}. Activos: {known}")
        return rec

    @staticmethod
    def _state(rec: dict) -> str:
        code = rec["proc"].poll()
        return "running" if code is None else ("done" if code == 0 else f"failed:{code}")

    def _public(self, rec: dict) -> dict:
        return {
            "id": rec["id"], "label": rec["label"], "command": rec["command"],
            "pid": rec["pid"], "state": self._state(rec), "log": rec["log"],
            "elapsed_s": round(time.time() - rec["started"], 1),
        }

    def _prune_locked(self) -> None:
        if len(self._jobs) <= self._max_jobs:
            return
        finished = [r for r in self._jobs.values() if r["proc"].poll() is not None]
        finished.sort(key=lambda r: r["started"])
        for rec in finished[: max(0, len(self._jobs) - self._max_jobs)]:
            self._jobs.pop(rec["id"], None)


_JOBS: JobRegistry | None = None


def get_jobs() -> JobRegistry:
    global _JOBS
    if _JOBS is None:
        _JOBS = JobRegistry()
    return _JOBS
