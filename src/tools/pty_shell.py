"""PTY opcional (pywinpty) con sesion persistente y fallback a PersistentShell.

El PTY es opt-in: se activa con ``NEXUS_PTY=1`` y el extra ``pywinpty``.
La sesion mantiene cwd, variables de entorno y procesos entre llamadas; si el
PTY no esta instalado o no arranca, ``run_command`` cae al shell persistente.
"""
from __future__ import annotations

import asyncio
import atexit
import os
import re
import secrets
import threading
import time
from typing import Any

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\|$)")
_PROMPT_RE = re.compile(r"__NEXUS_PROMPT__[^\n]*")


class PtyUnavailable(RuntimeError):
    pass


def _winpty():
    try:
        from winpty import PtyProcess  # type: ignore
        return PtyProcess
    except Exception:
        return None


def pty_available() -> bool:
    return _winpty() is not None


def pty_enabled() -> bool:
    return os.environ.get("NEXUS_PTY", "").strip().lower() in (
        "1", "true", "yes", "on")


def _clean(text: str) -> str:
    text = _OSC_RE.sub("", text or "")
    text = _CSI_RE.sub("", text)
    text = text.replace("\x1b\\", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _PROMPT_RE.sub("", text)
    text = text.replace("\x00", "")
    return text.strip("\n")


def _strip_echo(text: str, command: str) -> str:
    """Quita el eco de la consola (ConPTY refleja lo tecleado)."""
    target = command.strip()
    markers = ("echo __NEXUS_DONE_", "echo __NEXUS_READY_")
    out: list[str] = []
    removed = False
    for line in text.split("\n"):
        stripped = line.strip()
        if not removed and target and stripped == target:
            removed = True
            continue
        if any(stripped.startswith(m) for m in markers):
            continue
        if stripped == "@echo off" or stripped == "prompt __NEXUS_PROMPT__$P$G":
            continue
        out.append(line)
    return "\n".join(out).strip("\n")


class PtySession:
    """Un ``cmd.exe`` interactivo dentro de un ConPTY reutilizable."""

    def __init__(self, cwd: str = "", *, cols: int = 120, rows: int = 30):
        PtyProcess = _winpty()
        if PtyProcess is None:
            raise PtyUnavailable("pywinpty no esta instalado")
        self.cwd = cwd or os.getcwd()
        self._token = secrets.token_hex(8)
        self._ready = f"__NEXUS_READY_{self._token}__"
        self._done = f"__NEXUS_DONE_{self._token}__"
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._buffer = ""
        self._closed = False
        self._proc: Any = None
        try:
            self._proc = PtyProcess.spawn(
                ["cmd.exe", "/Q", "/K"],
                cwd=self.cwd,
                dimensions=(max(10, rows), max(20, cols)),
            )
        except Exception as e:
            raise PtyUnavailable(f"no se pudo iniciar el PTY: {e}") from e
        self._reader = threading.Thread(
            target=self._read_loop, name="nexus-pty-reader", daemon=True)
        self._reader.start()
        try:
            self._send("chcp 65001 >nul")
            self._send("@echo off")
            self._send("prompt __NEXUS_PROMPT__$P$G")
            self._send(f"echo {self._ready}")
            if not self._wait_ready(10.0):
                raise PtyUnavailable("el PTY no respondio al handshake")
        except Exception:
            self.close()
            raise

    @property
    def alive(self) -> bool:
        if self._closed or self._proc is None:
            return False
        try:
            return bool(self._proc.isalive())
        except Exception:
            return False

    def _read_loop(self) -> None:
        while not self._closed:
            try:
                data = self._proc.read(4096) if self._proc is not None else ""
            except EOFError:
                break
            except Exception:
                break
            if not data:
                continue
            with self._cv:
                self._buffer += data
                self._cv.notify_all()
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def _send(self, text: str) -> None:
        if not self.alive:
            raise PtyUnavailable("sesion PTY cerrada")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text.endswith("\n"):
            text += "\n"
        self._proc.write(text.replace("\n", "\r\n"))

    def _wait_ready(self, timeout_s: float) -> bool:
        pattern = re.compile(
            r"(?m)^" + re.escape(self._ready) + r"[ \t]*$")
        deadline = time.monotonic() + max(0.1, timeout_s)
        with self._cv:
            while True:
                if pattern.search(_clean(self._buffer)):
                    self._buffer = ""
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    return False
                self._cv.wait(min(remaining, 0.5))

    def _wait_done(self, timeout_s: float) -> tuple[str, int, bool]:
        pattern = re.compile(
            r"(?m)^" + re.escape(self._done) + r"(-?\d+)[ \t]*$")
        deadline = time.monotonic() + max(0.1, timeout_s)
        with self._cv:
            while True:
                clean = _clean(self._buffer)
                match = pattern.search(clean)
                if match:
                    output = clean[:match.start()].strip("\n")
                    self._buffer = ""
                    try:
                        code = int(match.group(1))
                    except Exception:
                        code = 1
                    return output, code, False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    output = clean
                    self._buffer = ""
                    return output, 124, True
                if self._closed or not self.alive:
                    output = clean
                    self._buffer = ""
                    return output, 1, False
                self._cv.wait(min(remaining, 0.5))

    def _exec_blocking(self, command: str, cwd: str,
                       timeout_s: float) -> tuple[str, str, int, bool]:
        with self._lock:
            if not self.alive:
                raise PtyUnavailable("sesion PTY cerrada")
            prefix = ""
            if cwd:
                prefix = f'cd /d "{cwd}" && '
            sent = prefix + command
            self._send(sent)
            self._send(f"echo {self._done}%errorlevel%")
            output, code, timed_out = self._wait_done(timeout_s)
            output = _strip_echo(output, sent)
            if timed_out:
                self.close()
                return output, "PTY timeout", 124, True
            return output, "", code, False

    async def exec(self, command: str, cwd: str = "",
                   timeout_s: float = 60.0) -> tuple[str, str, int, bool]:
        task = asyncio.create_task(asyncio.to_thread(
            self._exec_blocking, command, cwd, timeout_s))
        try:
            return await asyncio.wait_for(task, timeout=max(0.1, timeout_s) + 5.0)
        except asyncio.TimeoutError:
            self.close()
            return "", "PTY timeout", 124, True

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate(force=True)
        except Exception:
            pass
        try:
            proc.close(force=True)
        except Exception:
            pass


_SESSION: PtySession | None = None
_SESSION_LOCK = threading.Lock()


def get_pty_session(cwd: str = "") -> PtySession:
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None or not _SESSION.alive:
            _SESSION = PtySession(cwd or os.getcwd())
        return _SESSION


def reset_pty_session() -> None:
    global _SESSION
    with _SESSION_LOCK:
        session, _SESSION = _SESSION, None
    if session is not None:
        session.close()


async def run_pty(
    command: str,
    cwd: str = "",
    timeout_s: float = 60.0,
    *,
    cols: int = 120,
    rows: int = 30,
) -> tuple[str, str, int, bool]:
    """Ejecuta un comando en un PTY nuevo (sin reutilizar estado)."""
    session = PtySession(cwd, cols=cols, rows=rows)
    try:
        return await session.exec(command, cwd=cwd, timeout_s=timeout_s)
    finally:
        session.close()


async def run_command(
    command: str,
    cwd: str = "",
    timeout_s: float = 60.0,
    *,
    prefer_pty: bool | None = None,
) -> tuple[str, str, int, bool]:
    """PTY persistente si esta disponible/activo; si no, PersistentShell."""
    use_pty = pty_enabled() if prefer_pty is None else bool(prefer_pty)
    if use_pty and pty_available():
        try:
            session = get_pty_session(cwd)
            return await session.exec(command, cwd=cwd, timeout_s=timeout_s)
        except PtyUnavailable:
            pass
    from .persistent_shell import PersistentShell
    shell = PersistentShell.get_instance(cwd or os.getcwd())
    return await shell.exec(command, int(max(0.1, timeout_s) * 1000))


atexit.register(reset_pty_session)
