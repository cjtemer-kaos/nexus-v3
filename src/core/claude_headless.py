"""
Claude Code Headless — Control programático de Claude Code desde NEXUS.
Usa el token OAuth de la suscripción (NO necesita API key).

Permite al Director despachar tareas complejas a Claude Code CLI,
que tiene acceso a tools (Read, Edit, Bash, Grep, etc.) y skills.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# Token OAuth desde credentials (se refresca automáticamente)
_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def _get_oauth_token() -> Optional[str]:
    """Lee el token OAuth de las credenciales de Claude Code."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return os.environ["CLAUDE_CODE_OAUTH_TOKEN"]

    if _CREDENTIALS_PATH.exists():
        try:
            creds = json.loads(_CREDENTIALS_PATH.read_text(encoding="utf-8"))
            token = creds.get("claudeAiOauth", {}).get("accessToken", "")
            if token:
                return token
        except Exception as e:
            logger.error(f"Error reading Claude credentials: {e}")

    return None


@dataclass
class ClaudeResult:
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: float = 0
    tokens_used: int = 0


class ClaudeHeadless:
    """
    Ejecuta prompts en Claude Code CLI en modo headless.

    Usa el token OAuth de la suscripción del usuario.
    NO necesita API key de Anthropic.

    Uso:
        claude = ClaudeHeadless()
        result = await claude.run("Analiza este archivo", cwd="/ruta/proyecto")
    """

    def __init__(
        self,
        max_turns: int = 10,
        timeout: int = 300,
        allowed_tools: Optional[List[str]] = None,
        model: Optional[str] = None,
    ):
        self.max_turns = max_turns
        self.timeout = timeout
        self.allowed_tools = allowed_tools or [
            "Read", "Edit", "Write", "Bash", "Grep", "Glob",
        ]
        self.model = model  # Si es None, Claude usa su default (OAuth)
        self._token = _get_oauth_token()

        if not self._token:
            logger.warning("⚠️ No OAuth token found. Claude headless won't work.")

    async def run(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        max_turns: Optional[int] = None,
        allowed_tools: Optional[List[str]] = None,
        timeout: Optional[int] = None,
    ) -> ClaudeResult:
        """
        Ejecuta un prompt en Claude Code CLI headless.

        Args:
            prompt: El prompt a ejecutar
            cwd: Directorio de trabajo (default: supernexus-v2)
            max_turns: Máximo de turnos del agente
            allowed_tools: Tools permitidos
            timeout: Timeout en segundos
        """
        if not self._token:
            return ClaudeResult(success=False, error="No OAuth token available")

        start = datetime.now()
        turns = max_turns or self.max_turns
        tools = allowed_tools or self.allowed_tools
        tout = timeout or self.timeout
        work_dir = cwd or str(Path(__file__).resolve().parents[2])

        cmd = [
            "claude", "-p", prompt,
            "--max-turns", str(turns),
            "--allowedTools", ",".join(tools),
        ]

        if self.model:
            cmd.extend(["--model", self.model])

        env = os.environ.copy()
        env["CLAUDE_CODE_OAUTH_TOKEN"] = self._token

        try:
            logger.info(f"🤖 [CLAUDE-HEADLESS] Running: '{prompt[:80]}...' (max_turns={turns})")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
            )

            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=tout,
            )

            output = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            duration = (datetime.now() - start).total_seconds() * 1000

            # Filtrar warnings de stderr que no son errores reales
            real_errors = [
                line for line in err.split("\n")
                if line and "Warning:" not in line and "stdin" not in line
            ]

            if proc.returncode == 0:
                logger.info(f"✅ [CLAUDE-HEADLESS] Done in {duration:.0f}ms ({len(output)} chars)")
                return ClaudeResult(
                    success=True,
                    output=output,
                    duration_ms=duration,
                )
            else:
                error_msg = "\n".join(real_errors) if real_errors else f"Exit code {proc.returncode}"
                logger.error(f"❌ [CLAUDE-HEADLESS] Failed: {error_msg[:200]}")
                return ClaudeResult(
                    success=False,
                    output=output,
                    error=error_msg[:500],
                    duration_ms=duration,
                )

        except asyncio.TimeoutError:
            duration = (datetime.now() - start).total_seconds() * 1000
            logger.error(f"⏰ [CLAUDE-HEADLESS] Timeout after {tout}s")
            return ClaudeResult(
                success=False,
                error=f"Timeout after {tout}s",
                duration_ms=duration,
            )
        except FileNotFoundError:
            return ClaudeResult(
                success=False,
                error="Claude CLI not found. Install with: npm install -g @anthropic-ai/claude-code",
            )
        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ClaudeResult(
                success=False,
                error=str(e)[:500],
                duration_ms=duration,
            )

    async def run_streaming(
        self,
        prompt: str,
        cwd: Optional[str] = None,
        on_chunk: Optional[callable] = None,
    ) -> ClaudeResult:
        """
        Ejecuta con streaming JSON (output-format stream-json).
        Llama on_chunk(dict) por cada evento.
        """
        if not self._token:
            return ClaudeResult(success=False, error="No OAuth token available")

        start = datetime.now()
        work_dir = cwd or str(Path(__file__).resolve().parents[2])

        cmd = [
            "claude", "-p", prompt,
            "--max-turns", str(self.max_turns),
            "--output-format", "stream-json",
            "--allowedTools", ",".join(self.allowed_tools),
        ]

        env = os.environ.copy()
        env["CLAUDE_CODE_OAUTH_TOKEN"] = self._token

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
            )

            full_output = []
            async for line in proc.stdout:
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                    if on_chunk:
                        on_chunk(event)
                    # Capturar texto de respuesta
                    if event.get("type") == "assistant" and "content" in event:
                        for block in event["content"]:
                            if block.get("type") == "text":
                                full_output.append(block["text"])
                except json.JSONDecodeError:
                    full_output.append(text)

            await proc.wait()
            duration = (datetime.now() - start).total_seconds() * 1000

            return ClaudeResult(
                success=proc.returncode == 0,
                output="\n".join(full_output),
                duration_ms=duration,
            )

        except Exception as e:
            duration = (datetime.now() - start).total_seconds() * 1000
            return ClaudeResult(success=False, error=str(e)[:500], duration_ms=duration)
