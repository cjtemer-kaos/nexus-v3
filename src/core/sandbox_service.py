import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import time
import shlex
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger("nexus-sandbox")


@dataclass
class SandboxResult:
    success: bool = False
    output: str = ""
    error: str = ""
    return_code: int = -1
    duration_ms: float = 0.0


@dataclass
class SandboxConfig:
    max_concurrent: int = 5
    timeout_seconds: int = 30
    workdir: str = ""
    env_overrides: Dict[str, str] = field(default_factory=dict)
    # No heredar TODO el entorno: las API keys del harness no deben llegar al
    # subproceso. `inherit_env=True` es opt-in explícito.
    inherit_env: bool = False
    max_output_bytes: int = 200_000

    # Variables mínimas para que un intérprete/runtime arranque.
    _ENV_ALLOWLIST = (
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
        "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "NUMBER_OF_PROCESSORS",
        "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "SHELL", "PYTHONPATH",
    )


def _clamp(text: str, limit: int) -> str:
    """Cabeza + cola: el final de un build/test es donde está el error."""
    if not text or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return (text[:head] + f"\n[... {len(text) - head - tail} bytes recortados del medio ...]\n"
            + text[-tail:])


class SandboxService:
    def __init__(self, config: SandboxConfig = None):
        self.config = config or SandboxConfig()
        self._semaphore = asyncio.Semaphore(config.max_concurrent if config else 5)
        self._temp_dirs: Dict[str, str] = {}

    def _env(self, extra: dict | None = None) -> dict:
        """Allowlist de entorno (o herencia completa si se pidió explícitamente)."""
        if self.config.inherit_env:
            base = dict(os.environ)
        else:
            base = {k: os.environ[k] for k in self.config._ENV_ALLOWLIST if k in os.environ}
        base.update({str(k): str(v) for k, v in (self.config.env_overrides or {}).items()})
        if extra:
            base.update({str(k): str(v) for k, v in extra.items()})
        return base

    def _limit(self) -> int:
        return max(1000, int(self.config.max_output_bytes or 200_000))

    @staticmethod
    async def _kill_tree(proc) -> None:
        """Mata el árbol: en Windows `proc.kill()` deja huérfanos (npm/builds)."""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await proc.wait()
        except Exception:
            pass

    async def run_code(self, code: str, language: str = "python") -> SandboxResult:
        async with self._semaphore:
            start = time.time()
            ext_map = {"python": ".py", "javascript": ".js", "bash": ".sh", "go": ".go"}
            ext = ext_map.get(language, ".txt")
            tmp_dir = tempfile.mkdtemp(prefix="nexus-sandbox-")
            filepath = os.path.join(tmp_dir, f"script{ext}")
            try:
                with open(filepath, "w", encoding="utf-8") as f:
                    f.write(code)
                cmd = self._get_command(language, filepath)
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=self.config.workdir or tmp_dir,
                    env=self._env(),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.config.timeout_seconds)
                    return SandboxResult(
                        success=proc.returncode == 0,
                        output=_clamp(stdout.decode(errors="replace").strip(), self._limit()) if stdout else "",
                        error=_clamp(stderr.decode(errors="replace").strip(), self._limit()) if stderr else "",
                        return_code=proc.returncode or 0,
                        duration_ms=(time.time() - start) * 1000,
                    )
                except asyncio.TimeoutError:
                    await self._kill_tree(proc)
                    return SandboxResult(error=f"Timeout after {self.config.timeout_seconds}s", duration_ms=(time.time() - start) * 1000)
            except Exception as e:
                return SandboxResult(error=str(e), duration_ms=(time.time() - start) * 1000)
            finally:
                self._cleanup(tmp_dir)

    async def run_command(self, command: str) -> SandboxResult:
        async with self._semaphore:
            start = time.time()
            cmd_list = shlex.split(command)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd_list,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=self.config.workdir or os.getcwd(),
                    env=self._env(),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.config.timeout_seconds)
                    return SandboxResult(
                        success=proc.returncode == 0,
                        output=_clamp(stdout.decode(errors="replace").strip(), self._limit()) if stdout else "",
                        error=_clamp(stderr.decode(errors="replace").strip(), self._limit()) if stderr else "",
                        return_code=proc.returncode or 0,
                        duration_ms=(time.time() - start) * 1000,
                    )
                except asyncio.TimeoutError:
                    await self._kill_tree(proc)
                    return SandboxResult(error=f"Timeout after {self.config.timeout_seconds}s", duration_ms=(time.time() - start) * 1000)
            except Exception as e:
                return SandboxResult(error=str(e), duration_ms=(time.time() - start) * 1000)

    async def run_argv(self, argv: List[str], *, workdir: str = "", timeout: int = 0,
                       policy: str = "workspace_write", env: Dict[str, str] = None) -> SandboxResult:
        """Ejecuta argv SIN shell (create_subprocess_exec) con politica de alcance.

        policy='read_only' rechaza comandos mutantes/red por nombre de exe.
        return_code=-2 cuando la politica deniega.
        """
        if not argv or not isinstance(argv, list):
            return SandboxResult(error="argv vacio o invalido", return_code=-2)
        exe = os.path.basename(str(argv[0])).lower()
        if policy == "read_only":
            banned = {
                "rm", "rmdir", "mv", "cp", "dd", "mkfs", "chmod", "chown", "shutdown",
                "reboot", "kill", "taskkill", "format", "del", "erase", "curl", "wget",
                "pip", "pip3", "npm", "git", "sudo",
            }
            if exe in banned:
                return SandboxResult(error=f"policy=read_only: comando {exe!r} denegado", return_code=-2)
        async with self._semaphore:
            start = time.time()
            run_env = self._env(env)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *[str(a) for a in argv],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=workdir or self.config.workdir or os.getcwd(),
                    env=run_env,
                    start_new_session=(os.name != "nt"),
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=(timeout or self.config.timeout_seconds))
                    return SandboxResult(
                        success=proc.returncode == 0,
                        output=_clamp(stdout.decode(errors="replace").strip(), self._limit()) if stdout else "",
                        error=_clamp(stderr.decode(errors="replace").strip(), self._limit()) if stderr else "",
                        return_code=proc.returncode or 0,
                        duration_ms=(time.time() - start) * 1000,
                    )
                except asyncio.TimeoutError:
                    await self._kill_tree(proc)
                    return SandboxResult(
                        error=f"Timeout after {timeout or self.config.timeout_seconds}s",
                        return_code=-1, duration_ms=(time.time() - start) * 1000)
            except Exception as e:
                return SandboxResult(error=str(e), duration_ms=(time.time() - start) * 1000)

    def _get_command(self, language: str, filepath: str) -> List[str]:
        commands = {
            "python": ["python", filepath],
            "javascript": ["node", filepath],
            "bash": ["bash", filepath],
            "go": ["go", "run", filepath],
        }
        return commands.get(language, ["python", filepath])

    def _cleanup(self, tmp_dir: str):
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def get_stats(self) -> Dict:
        return {
            "active_slots": self._semaphore._value,
            "max_concurrent": self.config.max_concurrent,
            "config": {
                "max_concurrent": self.config.max_concurrent,
                "timeout": self.config.timeout_seconds,
            },
        }
