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


class SandboxService:
    def __init__(self, config: SandboxConfig = None):
        self.config = config or SandboxConfig()
        self._semaphore = asyncio.Semaphore(config.max_concurrent if config else 5)
        self._temp_dirs: Dict[str, str] = {}

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
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.config.timeout_seconds)
                    return SandboxResult(
                        success=proc.returncode == 0,
                        output=stdout.decode().strip() if stdout else "",
                        error=stderr.decode().strip() if stderr else "",
                        return_code=proc.returncode or 0,
                        duration_ms=(time.time() - start) * 1000,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
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
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=self.config.timeout_seconds)
                    return SandboxResult(
                        success=proc.returncode == 0,
                        output=stdout.decode().strip() if stdout else "",
                        error=stderr.decode().strip() if stderr else "",
                        return_code=proc.returncode or 0,
                        duration_ms=(time.time() - start) * 1000,
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    return SandboxResult(error=f"Timeout after {self.config.timeout_seconds}s", duration_ms=(time.time() - start) * 1000)
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
