"""Prompt template loading (lethe parity)."""
from __future__ import annotations
from pathlib import Path
from typing import Dict

def _workspace_dir() -> Path:
    return Path("workspace")
def _config_dir() -> Path:
    return Path("config")

def _candidate_paths(name: str) -> list[Path]:
    base = name if name.endswith(".md") else f"{name}.md"
    ws = _workspace_dir()
    cfg = _config_dir()
    return [ws / "prompts" / base, cfg / "prompts" / base, cfg / "workspace" / "prompts" / base]

def load_prompt_template(name: str, fallback: str = "") -> str:
    for path in _candidate_paths(name):
        try:
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except Exception:
            continue
    return fallback

def render_prompt_template(name: str, variables: Dict[str, object], fallback: str = "") -> str:
    template = load_prompt_template(name, fallback=fallback)
    if not template:
        return ""
    return template.format(**variables)
