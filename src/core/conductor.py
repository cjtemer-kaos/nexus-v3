"""
Conductor — Parallel Worktree Coordinator (gstack pattern).

Gestiona multiples streams de trabajo en git worktrees aislados,
permitiendo paralelismo real sin conflictos de merge.

Inspirado en Conductor (macOS app usada por Garry Tan).
Adaptado para NEXUS: usa subprocess + git worktree, no requiere app externa.

Flujo:
  1. conductor.spawn("feature-x", goal="...") -> crea worktree + branch
  2. conductor.status() -> ver estado de todos los streams
  3. conductor.merge("feature-x") -> merge a main con review gate
  4. conductor.cleanup("feature-x") -> elimina worktree

Integracion con DevLoop:
  Cada stream puede correr su propio DevLoop de 7 fases independientemente.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class StreamStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    REVIEW = "review"      # esperando review antes de merge
    MERGING = "merging"
    MERGED = "merged"
    FAILED = "failed"
    CLEANED = "cleaned"


@dataclass
class WorkStream:
    id: str
    name: str
    goal: str
    branch: str
    worktree_path: str
    status: StreamStatus = StreamStatus.CREATED
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = ""
    commits: list[str] = field(default_factory=list)
    phase: str = ""       # current DevLoop phase if running
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "goal": self.goal[:80],
            "branch": self.branch,
            "status": self.status.value,
            "commits": len(self.commits),
            "phase": self.phase,
            "worktree": self.worktree_path,
        }


class Conductor:
    """
    Coordina multiples work streams en git worktrees paralelos.

    Uso:
        conductor = Conductor(repo_path=os.environ.get("NEXUS_PROJECT_DIR", "."))
        stream = await conductor.spawn("auth-refactor", goal="Refactorizar auth middleware")
        # ... trabajo en el worktree ...
        await conductor.merge("auth-refactor")
        await conductor.cleanup("auth-refactor")
    """

    def __init__(
        self,
        repo_path: str,
        worktree_base: str | None = None,
        main_branch: str = "main",
        max_parallel: int = 4,
        on_merge: Callable[[WorkStream], Awaitable[None]] | None = None,
    ):
        self.repo_path = Path(repo_path)
        self.worktree_base = Path(worktree_base or str(self.repo_path / ".worktrees"))
        self.main_branch = main_branch
        self.max_parallel = max_parallel
        self.on_merge = on_merge
        self._streams: dict[str, WorkStream] = {}

    async def spawn(self, name: str, goal: str = "", base_branch: str | None = None) -> WorkStream:
        """Crea un nuevo work stream con worktree aislado."""
        if name in self._streams and self._streams[name].status not in (StreamStatus.CLEANED, StreamStatus.MERGED):
            raise ValueError(f"Stream '{name}' already exists and is active")

        active = [s for s in self._streams.values() if s.status in (StreamStatus.RUNNING, StreamStatus.CREATED)]
        if len(active) >= self.max_parallel:
            raise RuntimeError(f"Max parallel streams ({self.max_parallel}) reached")

        branch = f"conductor/{name}"
        worktree_path = str(self.worktree_base / name)
        stream_id = str(uuid.uuid4())[:8]

        # Ensure worktree base exists
        self.worktree_base.mkdir(parents=True, exist_ok=True)

        # Create branch from base
        base = base_branch or self.main_branch
        try:
            self._git("branch", "-D", branch, check=False)  # clean stale branch
            self._git("branch", branch, base)
            self._git("worktree", "add", worktree_path, branch)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to create worktree: {e.stderr}") from e

        stream = WorkStream(
            id=stream_id,
            name=name,
            goal=goal,
            branch=branch,
            worktree_path=worktree_path,
            status=StreamStatus.CREATED,
        )
        self._streams[name] = stream
        logger.info(f"Conductor: spawned stream '{name}' -> {worktree_path}")
        return stream

    async def merge(self, name: str, squash: bool = True) -> bool:
        """Merge un stream a main. Squash por defecto para commits limpios."""
        stream = self._get_stream(name)
        if stream.status == StreamStatus.MERGED:
            return True

        stream.status = StreamStatus.MERGING
        stream.updated_at = datetime.now().isoformat()

        try:
            # Get commits in branch
            log_output = self._git(
                "log", f"{self.main_branch}..{stream.branch}",
                "--oneline", "--no-decorate"
            )
            stream.commits = [line.strip() for line in log_output.splitlines() if line.strip()]

            if not stream.commits:
                logger.info(f"Stream '{name}' has no commits to merge")
                stream.status = StreamStatus.MERGED
                return True

            # Merge
            if squash:
                self._git("merge", "--squash", stream.branch)
                commit_msg = f"feat({name}): {stream.goal[:60]}\n\nSquashed {len(stream.commits)} commits from conductor/{name}"
                self._git("commit", "-m", commit_msg)
            else:
                self._git("merge", "--no-ff", stream.branch, "-m",
                         f"Merge conductor/{name}: {stream.goal[:60]}")

            stream.status = StreamStatus.MERGED
            stream.updated_at = datetime.now().isoformat()

            if self.on_merge:
                await self.on_merge(stream)

            logger.info(f"Conductor: merged '{name}' ({len(stream.commits)} commits)")
            return True

        except subprocess.CalledProcessError as e:
            stream.status = StreamStatus.FAILED
            stream.error = e.stderr or str(e)
            logger.error(f"Conductor: merge failed for '{name}': {stream.error}")
            # Abort merge if in progress
            self._git("merge", "--abort", check=False)
            return False

    async def cleanup(self, name: str) -> None:
        """Elimina worktree y branch de un stream."""
        stream = self._get_stream(name)

        try:
            self._git("worktree", "remove", stream.worktree_path, "--force", check=False)
            self._git("branch", "-D", stream.branch, check=False)
        except Exception as e:
            logger.warning(f"Cleanup warning for '{name}': {e}")

        stream.status = StreamStatus.CLEANED
        stream.updated_at = datetime.now().isoformat()
        logger.info(f"Conductor: cleaned up '{name}'")

    async def pause(self, name: str) -> None:
        stream = self._get_stream(name)
        stream.status = StreamStatus.PAUSED
        stream.updated_at = datetime.now().isoformat()

    async def resume(self, name: str) -> None:
        stream = self._get_stream(name)
        if stream.status == StreamStatus.PAUSED:
            stream.status = StreamStatus.RUNNING
            stream.updated_at = datetime.now().isoformat()

    def get_stream(self, name: str) -> WorkStream | None:
        return self._streams.get(name)

    def active_streams(self) -> list[WorkStream]:
        return [
            s for s in self._streams.values()
            if s.status in (StreamStatus.CREATED, StreamStatus.RUNNING, StreamStatus.REVIEW, StreamStatus.PAUSED)
        ]

    def status(self) -> dict:
        """Estado completo del conductor."""
        return {
            "repo": str(self.repo_path),
            "main_branch": self.main_branch,
            "max_parallel": self.max_parallel,
            "total_streams": len(self._streams),
            "active": len(self.active_streams()),
            "streams": {
                name: s.summary() for name, s in self._streams.items()
            },
        }

    def _get_stream(self, name: str) -> WorkStream:
        if name not in self._streams:
            raise KeyError(f"Stream '{name}' not found")
        return self._streams[name]

    def _git(self, *args: str, check: bool = True) -> str:
        """Ejecuta git command en el repo principal."""
        cmd = ["git", "-C", str(self.repo_path)] + list(args)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd,
                output=result.stdout, stderr=result.stderr
            )
        return result.stdout.strip()


class ConductorDevLoop:
    """
    Integra Conductor + DevLoop para ejecutar loops de 7 fases en worktrees paralelos.

    Uso:
        cdl = ConductorDevLoop(repo_path="...", llm_call=my_fn)
        results = await cdl.run_parallel([
            ("auth-refactor", "Refactorizar auth middleware"),
            ("api-cache", "Agregar cache a endpoints pesados"),
        ])
    """

    def __init__(
        self,
        repo_path: str,
        llm_call: Callable[[str], Awaitable[str]],
        max_parallel: int = 3,
    ):
        from src.core.dev_loop import DevLoop
        self.conductor = Conductor(repo_path=repo_path, max_parallel=max_parallel)
        self.llm_call = llm_call
        self._DevLoop = DevLoop

    async def run_parallel(
        self,
        tasks: list[tuple[str, str]],
    ) -> dict[str, Any]:
        """Ejecuta multiples DevLoops en paralelo, cada uno en su worktree."""
        streams = []
        for name, goal in tasks:
            stream = await self.conductor.spawn(name, goal=goal)
            stream.status = StreamStatus.RUNNING
            streams.append(stream)

        async def run_stream(stream: WorkStream) -> dict:
            loop = self._DevLoop(
                llm_call=self.llm_call,
                context=f"Worktree: {stream.worktree_path}\nBranch: {stream.branch}",
            )
            try:
                result = await loop.run(stream.goal)
                stream.phase = result.current_phase.value
                return {
                    "name": stream.name,
                    "status": result.status,
                    "phases": {k: v.gate_result.value for k, v in result.phases.items()},
                    "duration_s": result.total_duration_s,
                }
            except Exception as e:
                stream.status = StreamStatus.FAILED
                stream.error = str(e)
                return {"name": stream.name, "status": "failed", "error": str(e)}

        results = await asyncio.gather(
            *[run_stream(s) for s in streams],
            return_exceptions=True,
        )

        return {
            "conductor": self.conductor.status(),
            "results": [
                r if isinstance(r, dict) else {"error": str(r)}
                for r in results
            ],
        }
