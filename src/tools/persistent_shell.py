"""
Persistent Shell - Singleton shell for command execution with state preservation.
Based on opencode/internal/llm/tools/shell/shell.go pattern.

Uses temp files to capture stdout/stderr/exit code/cwd across commands.
Maintains a single shell process with command queue for async execution.
"""

import asyncio
import os
import re
import subprocess
import tempfile
import threading
import time
from typing import Optional, Tuple


class PersistentShell:
    """Singleton persistent shell with command queue and state tracking."""

    _instance: Optional["PersistentShell"] = None
    _lock = threading.Lock()

    # Blacklist de comandos destructivos
    DESTRUCTIVE_COMMANDS = [
        "rm -rf /", "rm -rf /*", "rm -rf ~",
        "dd if=/dev/zero", "dd if=/dev/random",
        "mkfs", "fdisk", "parted",
        "format c:", "diskpart",
        ":(){ :|:& };:",  # fork bomb
        "shutdown -h now", "shutdown -r now",
        "poweroff", "reboot",
        "wget.*\\|.*sh", "curl.*\\|.*sh",  # pipe download+execute
        "chmod -R 777 /", "chmod -R 000 /",
        "chown -R",
        "iptables -F", "iptables -X",
        "systemctl stop ssh", "systemctl disable ssh",
    ]

    # Patrones de inyeccion
    INJECTION_PATTERNS = [
        r";\s*rm\s", r"\|\|\s*rm\s", r"&&\s*rm\s",
        r";\s*dd\s", r"\|\s*bash", r"\|\|\s*bash",
        r"`.*`", r"\$\(.*\)",  # command substitution
    ]

    @classmethod
    def is_safe_command(cls, command: str) -> Tuple[bool, str]:
        """Verifica si un comando es seguro para ejecutar"""
        # Normalize: collapse multiple whitespace to single, strip, lowercase
        cmd_normalized = re.sub(r'\s+', ' ', command.lower().strip())

        # Check destructive commands
        for pattern in cls.DESTRUCTIVE_COMMANDS:
            if pattern.lower() in cmd_normalized:
                return False, f"Command blocked: matches destructive pattern '{pattern}'"

        # Check injection patterns
        for pattern in cls.INJECTION_PATTERNS:
            if re.search(pattern, cmd_normalized):
                return False, f"Command blocked: matches injection pattern '{pattern}'"

        return True, ""

    def __init__(self, cwd: str = ""):
        self.cwd = cwd or os.getcwd()
        self.is_alive = False
        self._proc: Optional[subprocess.Popen] = None
        self._running = False
        self._mutex = threading.Lock()
        self._start_shell()

    def _start_shell(self):
        """Start the shell process."""
        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

            self._proc = subprocess.Popen(
                ["cmd.exe"] if os.name == "nt" else ["/bin/bash", "-l"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,  # stdout captured via temp files
                stderr=subprocess.PIPE,  # capture stderr for diagnostics
                cwd=self.cwd,
                creationflags=creationflags,
                text=True,
            )
            self.is_alive = True
        except Exception:
            self.is_alive = False

    @classmethod
    def get_instance(cls, cwd: str = "") -> "PersistentShell":
        """Get or create the singleton instance."""
        with cls._lock:
            if cls._instance is None or not cls._instance.is_alive:
                cls._instance = cls(cwd or os.getcwd())
            return cls._instance

    async def exec(
        self,
        command: str,
        timeout_ms: int = 60000,
    ) -> Tuple[str, str, int, bool]:
        """
        Execute a command in the persistent shell.
        Returns (stdout, stderr, exit_code, interrupted).
        """
        if not self.is_alive:
            return "", "Shell is not alive", 1, False

        # Verificar seguridad del comando
        is_safe, reason = PersistentShell.is_safe_command(command)
        if not is_safe:
            return "", f"Security: {reason}", 1, False

        timeout = timeout_ms / 1000.0
        temp_dir = tempfile.gettempdir()
        ts = str(time.time_ns())

        stdout_file = os.path.join(temp_dir, f"nexus-stdout-{ts}")
        stderr_file = os.path.join(temp_dir, f"nexus-stderr-{ts}")
        status_file = os.path.join(temp_dir, f"nexus-status-{ts}")
        cwd_file = os.path.join(temp_dir, f"nexus-cwd-{ts}")

        try:
            if os.name == "nt":
                full_command = (
                    f"({command}) > {stdout_file} 2> {stderr_file}\n"
                    f"echo %ERRORLEVEL% > {status_file}\n"
                    f"cd > {cwd_file}\n"
                )
            else:
                full_command = (
                    f"({command}) > {stdout_file} 2> {stderr_file}\n"
                    f"echo $? > {status_file}\n"
                    f"pwd > {cwd_file}\n"
                )

            with self._mutex:
                if not self.is_alive or not self._proc or not self._proc.stdin:
                    return "", "Shell is not alive", 1, False
                self._proc.stdin.write(full_command)
                self._proc.stdin.flush()

            start = time.time()
            interrupted = False
            poll_interval = 0.05

            while time.time() - start < timeout:
                if os.path.exists(status_file) and os.path.getsize(status_file) > 0:
                    break
                await asyncio.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.5, 0.5)
            else:
                interrupted = True
                try:
                    if self._proc:
                        self._proc.kill()
                except Exception:
                    pass
                self._start_shell()

            stdout = ""
            stderr = ""
            exit_code = 0

            try:
                if os.path.exists(stdout_file):
                    with open(stdout_file, "r", errors="replace") as f:
                        stdout = f.read()
                if os.path.exists(stderr_file):
                    with open(stderr_file, "r", errors="replace") as f:
                        stderr = f.read()
                if os.path.exists(status_file):
                    with open(status_file, "r") as f:
                        code_str = f.read().strip()
                        try:
                            exit_code = int(code_str)
                        except (ValueError, TypeError):
                            if interrupted:
                                exit_code = 143
                if os.path.exists(cwd_file):
                    with open(cwd_file, "r") as f:
                        new_cwd = f.read().strip()
                        if new_cwd and os.path.isdir(new_cwd):
                            self.cwd = new_cwd
            except Exception:
                pass

            if interrupted:
                stderr += "\nCommand timed out"

            return stdout, stderr, exit_code, interrupted

        finally:
            for f in [stdout_file, stderr_file, status_file, cwd_file]:
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except Exception:
                    pass

    def close(self):
        """Close the shell process."""
        with self._mutex:
            if self.is_alive and self._proc:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.write("exit\n")
                        self._proc.stdin.flush()
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            self.is_alive = False
            PersistentShell._instance = None
