"""
File-Based Prompt Loader - Dynamic prompt loading from filesystem.
Absorbed from openswarm pattern — names cleaned.
"""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class PromptLoader:
    """Load prompts from filesystem with dynamic context injection."""

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path(os.environ.get("APP_DATA", Path.home() / ".app")) / "prompts"
        self._cache: Dict[str, str] = {}

    def load_prompt(self, name: str, injectors: Optional[List[Callable[[], str]]] = None) -> str:
        """Load prompt from file, optionally with dynamic context injection."""
        prompt = self._load_from_file(name)

        if not prompt:
            return ""

        if injectors:
            extras = []
            for inj in injectors:
                try:
                    result = inj()
                    if result:
                        extras.append(result)
                except Exception as e:
                    logger.warning(f"Prompt injector failed: {e}")
            if extras:
                prompt = prompt + "\n\n" + "\n\n".join(extras)

        return prompt

    def _load_from_file(self, name: str) -> str:
        if name in self._cache:
            return self._cache[name]

        prompt_file = self.base_dir / f"{name}.md"
        if not prompt_file.exists():
            prompt_file = self.base_dir / f"{name}.txt"
        if not prompt_file.exists():
            return ""

        try:
            content = prompt_file.read_text(encoding="utf-8")
            self._cache[name] = content
            return content
        except Exception as e:
            logger.error(f"Failed to load prompt {name}: {e}")
            return ""

    def invalidate(self, name: str = ""):
        if name:
            self._cache.pop(name, None)
        else:
            self._cache.clear()

    def list_prompts(self) -> List[str]:
        if not self.base_dir.exists():
            return []
        return [f.stem for f in self.base_dir.glob("*.md")] + [f.stem for f in self.base_dir.glob("*.txt")]

    def save_prompt(self, name: str, content: str, ext: str = "md"):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        prompt_file = self.base_dir / f"{name}.{ext}"
        prompt_file.write_text(content, encoding="utf-8")
        self._cache.pop(name, None)


def utc_now_injector() -> str:
    """Injector: current UTC date/time."""
    return f"Current date/time (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"


def project_list_injector(projects_dir: Optional[Path] = None) -> Callable[[], str]:
    """Injector: list of existing project folders."""
    def _inject() -> str:
        if not projects_dir or not projects_dir.exists():
            return ""
        folders = [f.name for f in projects_dir.iterdir() if f.is_dir() and not f.name.startswith(".")]
        if not folders:
            return ""
        return "Existing project folders (do NOT reuse these names):\n" + "\n".join(f"  - {f}" for f in sorted(folders))
    return _inject
