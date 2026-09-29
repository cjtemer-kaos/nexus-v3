"""
Auto-commit reflexivo — patrón extraído de Aider.
Genera commit messages con el Director (gratis) y auto-commitea cambios.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, List, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

NEXUS_ROOT = Path(os.environ.get("NEXUS_PROJECT_ROOT", str(Path(__file__).resolve().parents[2])))

# Aider pattern: attribution via Co-authored-by
COMMIT_TRAILER = "Co-authored-by: NEXUS-Director <nexus@supernexus.local>"


def _git(*args: str, cwd: Optional[Path] = None) -> Tuple[int, str]:
    """Run git command, return (returncode, stdout)."""
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd or NEXUS_ROOT),
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode, result.stdout.strip()


def get_dirty_files(cwd: Optional[Path] = None) -> List[str]:
    """Get list of modified/added files (not untracked)."""
    code, out = _git("diff", "--name-only", cwd=cwd)
    staged_code, staged_out = _git("diff", "--cached", "--name-only", cwd=cwd)
    files = set()
    if code == 0 and out:
        files.update(out.splitlines())
    if staged_code == 0 and staged_out:
        files.update(staged_out.splitlines())
    return sorted(files)


def get_diff_summary(cwd: Optional[Path] = None) -> str:
    """Get a compact diff for commit message generation."""
    code, out = _git("diff", "--stat", cwd=cwd)
    if code != 0:
        return ""
    # Truncate if too long (save tokens for Director)
    lines = out.splitlines()
    if len(lines) > 20:
        lines = lines[:18] + [f"... y {len(lines) - 18} archivos más"]
    return "\n".join(lines)


async def generate_commit_message(diff_summary: str, ollama_client=None) -> str:
    """Use Director (deepseek-r1:8b, FREE) to generate commit message."""
    if not diff_summary:
        return "chore: auto-commit sin cambios significativos"

    if ollama_client:
        try:
            prompt = (
                "Genera UN mensaje de commit conciso en español para estos cambios. "
                "Formato: tipo(scope): descripción. Tipos: feat, fix, refactor, chore, docs. "
                "Solo responde el mensaje, nada más.\n\n"
                f"Cambios:\n{diff_summary[:500]}"
            )
            response = await ollama_client.generate(
                model="deepseek-r1:8b",
                prompt=prompt,
                max_tokens=100,
                temperature=0.3,
            )
            msg = response.strip().strip('"').strip("'")
            # Clean thinking tags if deepseek adds them
            if "<think>" in msg:
                msg = msg.split("</think>")[-1].strip()
            if msg and len(msg) < 200:
                return msg
        except Exception as e:
            logger.warning(f"Director commit-msg fallback: {e}")

    # Fallback: deterministic message from diff
    lines = diff_summary.splitlines()
    file_count = len([line for line in lines if line.strip() and "|" in line])
    return f"chore: auto-commit {file_count} archivos modificados"


async def auto_commit(
    message: Optional[str] = None,
    files: Optional[List[str]] = None,
    cwd: Optional[Path] = None,
    ollama_client=None,
    add_all: bool = False,
) -> Optional[dict]:
    """
    Auto-commit pattern from Aider:
    1. Detect dirty files
    2. Generate AI commit message (FREE via Director)
    3. Commit with attribution trailer
    """
    target = cwd or NEXUS_ROOT

    # Check if git repo
    code, _ = _git("rev-parse", "--is-inside-work-tree", cwd=target)
    if code != 0:
        return None

    dirty = files or get_dirty_files(cwd=target)
    if not dirty and not add_all:
        return None

    # Stage files
    if add_all:
        _git("add", "-A", cwd=target)
    else:
        for f in dirty:
            _git("add", f, cwd=target)

    # Generate message
    if not message:
        diff_summary = get_diff_summary(cwd=target)
        message = await generate_commit_message(diff_summary, ollama_client)

    # Commit with trailer (Aider pattern)
    full_message = f"{message}\n\n{COMMIT_TRAILER}"
    code, out = _git("commit", "-m", full_message, cwd=target)

    if code == 0:
        # Get commit hash
        _, commit_hash = _git("rev-parse", "--short", "HEAD", cwd=target)
        logger.info(f"Auto-commit: {commit_hash} — {message}")
        return {
            "hash": commit_hash,
            "message": message,
            "files": dirty,
            "timestamp": datetime.now().isoformat(),
        }
    else:
        logger.warning(f"Auto-commit failed: {out}")
        return None


def get_recent_commits(n: int = 10, cwd: Optional[Path] = None) -> List[dict]:
    """Get recent commits for context."""
    code, out = _git(
        "log", f"-{n}", "--format=%H|%s|%an|%ai",
        cwd=cwd or NEXUS_ROOT,
    )
    if code != 0 or not out:
        return []
    commits = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "hash": parts[0][:8],
                "message": parts[1],
                "author": parts[2],
                "date": parts[3],
            })
    return commits
