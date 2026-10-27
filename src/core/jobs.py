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
from typing import Any


def _jobs_root() -> Path:
    try:
        from .environment import data_home
        return data_home() / "jobs"
    except Exception:
        return Path(__file__).parents[2] / "data" / "jobs"


# Techo por lectura. Sin techo, `job_read(max_chars=10**12)` hacia que
# `fh.read(n)` intentara reservar ese tamano: MemoryError silencioso (el except
# de read lo tragaba) y el agente recibia `output: ""` con `pending_bytes` > 0,
# o sea un job con salida que parecia mudo para siempre.
MAX_READ_CHARS = 200_000
DEFAULT_READ_CHARS = 20_000


def _clamp_chars(value: Any, default: int = DEFAULT_READ_CHARS) -> int:
    """Cuantos bytes leer: [4, MAX_READ_CHARS]. 0/negativo/no numerico = default.

    El piso es 4 (un caracter UTF-8 completo): con menos, el tramo siempre
    terminaria a mitad de un caracter.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return max(4, min(n, MAX_READ_CHARS))


def _decode_tramo(raw: bytes) -> tuple[str, int]:
    """Decodifica UTF-8 sin partir un caracter multibyte al final del tramo.

    Devuelve (texto, bytes_consumidos). El corte de `max_chars` caia donde cayera:
    si partia un `á` (2 bytes), la salida traia U+FFFD y el siguiente tramo
    arrancaba en medio del caracter, otro U+FFFD. Con logs en castellano eso es
    corrupcion constante. Se retrocede hasta 3 bytes para terminar en un limite
    de caracter y el cursor queda ahi.

    Solo se retrocede si el error es una secuencia INCOMPLETA al final. Un byte
    invalido de verdad se decodifica con reemplazo y se consume entero: si no,
    un log binario avanzaria de a 1 byte por lectura y el agente quedaria
    pidiendo tramos hasta que el anti-loop lo corte.
    """
    if not raw:
        return "", 0
    try:
        return raw.decode("utf-8"), len(raw)
    except UnicodeDecodeError as e:
        if e.reason.startswith("unexpected end of data"):
            for back in range(1, 4):
                corte = len(raw) - back
                if corte <= 0:
                    break
                try:
                    return raw[:corte].decode("utf-8"), corte
                except UnicodeDecodeError:
                    continue
        # Salida de un comando de consola: en Windows suele ser la code page OEM
        # (cp437/cp850), no UTF-8. Con `replace` cada acento quedaba U+FFFD.
        from src.core.file_text import decode_output
        return decode_output(raw)[0], len(raw)


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

    def read(self, job_id: str, cursor: int = 0, max_chars: int = DEFAULT_READ_CHARS) -> dict:
        rec = self._get_rec(job_id)
        log = Path(rec["log"])
        # Solo el tramo pedido: antes hacia `log.read_bytes()` y cargaba el log
        # ENTERO en memoria en cada lectura. Con un build verboso (logs de
        # cientos de MB) cada job_read/job_wait copiaba todo el archivo.
        try:
            size = log.stat().st_size if log.exists() else 0
        except Exception:
            size = 0
        want = _clamp_chars(max_chars)
        try:
            start = max(0, int(cursor or 0))
        except (TypeError, ValueError):
            start = 0
        reseteado = start > size
        if reseteado:
            # Cursor de un log que ya no esta (backend reiniciado con el job
            # reusado) o inventado por el modelo: leer desde 0 y decirlo, en vez
            # de devolver "" para siempre con pending_bytes 0.
            start = 0
        chunk = b""
        if size > start:
            try:
                with open(log, "rb") as fh:
                    fh.seek(start)
                    chunk = fh.read(want)
            except Exception:
                chunk = b""
        texto, consumidos = _decode_tramo(chunk)
        end = start + consumidos
        out = {
            **self._public(rec),
            "cursor": end,
            "next_cursor": end,
            "pending_bytes": max(0, size - end),
            "output": texto,
        }
        if reseteado:
            out["reset"] = True
        return out

    def wait(self, job_id: str, timeout_s: float = 30.0,
             cursor: int = 0, max_chars: int = 30_000) -> dict:
        rec = self._get_rec(job_id)
        deadline = time.time() + max(0.0, min(300.0, float(timeout_s)))
        while time.time() < deadline:
            if rec["proc"].poll() is not None:
                break
            time.sleep(0.2)
        # `cursor` se respeta: antes `wait` leia siempre desde 0, asi que un agente
        # que ya habia leido 50 KB volvia a recibir la cabeza (y su cursor
        # retrocedia a 30000, lo que invita a re-leer en circulo).
        return self.read(job_id, cursor=cursor, max_chars=max_chars)

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
    def kill_all(self) -> int:
        """Mata los jobs vivos; devuelve cuántos. Se llama en el shutdown del server.

        `CREATE_NEW_PROCESS_GROUP` desacopla al job del padre a propósito (para que
        un Ctrl+C en el harness no lo corte), así que el cierre tiene que ser explícito.
        """
        with self._lock:
            ids = [jid for jid, rec in self._jobs.items() if rec["proc"].poll() is None]
        matados = 0
        for jid in ids:
            try:
                self.kill(jid)
                matados += 1
            except Exception:
                continue
        return matados

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
