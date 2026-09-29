"""
Custom Commands — F19: Markdown-based reusable prompts with variable substitution

Users create .md files with prompts that can be executed with variables.
Supports user-level and project-level scopes.
"""

import logging
import re
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-commands")


@dataclass
class CustomCommand:
    name: str
    description: str
    prompt: str
    variables: List[str]
    scope: str  # "user" or "project"
    created_at: str = ""
    usage_count: int = 0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def render(self, variables: Dict[str, str] = None) -> str:
        prompt = self.prompt
        variables = variables or {}
        for var in self.variables:
            value = variables.get(var, variables.get(var.upper(), ""))
            if not value:
                raise ValueError(f"Missing required variable: {var}")
            prompt = prompt.replace(f"${{{var}}}", value)
            prompt = prompt.replace(f"${{{var.upper()}}}", value)
        return prompt


class CustomCommandManager:
    """Manages custom markdown-based commands"""

    def __init__(self, user_dir: Optional[str] = None, project_dir: Optional[str] = None):
        if user_dir is None:
            user_dir = str(Path.home() / ".nexus" / "commands")
        if project_dir is None:
            project_dir = str(Path.cwd() / ".nexus" / "commands")

        self.user_dir = Path(user_dir)
        self.project_dir = Path(project_dir)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self._commands: Dict[str, CustomCommand] = {}
        self._load_commands()

    def _load_commands(self):
        for cmd_dir, scope in [(self.user_dir, "user"), (self.project_dir, "project")]:
            if cmd_dir.exists():
                for md_file in cmd_dir.glob("*.md"):
                    cmd = self._parse_command(md_file, scope)
                    if cmd:
                        self._commands[cmd.name] = cmd

    def _parse_command(self, file_path: Path, scope: str) -> Optional[CustomCommand]:
        try:
            content = file_path.read_text(encoding="utf-8")

            # Parse frontmatter: --- ... ---
            match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", content, re.DOTALL)
            if match:
                frontmatter = match.group(1)
                prompt = match.group(2).strip()
            else:
                frontmatter = ""
                prompt = content.strip()

            # Extract metadata from frontmatter
            name_match = re.search(r"name:\s*(.+)", frontmatter)
            desc_match = re.search(r"description:\s*(.+)", frontmatter)

            name = name_match.group(1).strip() if name_match else file_path.stem
            description = desc_match.group(1).strip() if desc_match else ""

            # Extract variables ($VAR or ${VAR})
            variables = list(set(re.findall(r"\$\{?(\w+)\}?", prompt)))

            return CustomCommand(
                name=name,
                description=description,
                prompt=prompt,
                variables=variables,
                scope=scope,
            )
        except Exception as e:
            logger.warning(f"Failed to parse command {file_path}: {e}")
            return None

    def create_command(self, name: str, prompt: str, description: str = "", scope: str = "user") -> CustomCommand:
        variables = list(set(re.findall(r"\$\{?(\w+)\}?", prompt)))
        cmd = CustomCommand(
            name=name,
            description=description,
            prompt=prompt,
            variables=variables,
            scope=scope,
        )
        self._commands[name] = cmd

        # Save to file
        target_dir = self.user_dir if scope == "user" else self.project_dir
        file_path = target_dir / f"{name}.md"
        file_path.write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n{prompt}\n",
            encoding="utf-8",
        )
        logger.info(f"Custom command created: {name}")
        return cmd

    def get_command(self, name: str) -> Optional[CustomCommand]:
        return self._commands.get(name)

    def execute_command(self, name: str, variables: Dict[str, str] = None) -> Optional[str]:
        cmd = self.get_command(name)
        if not cmd:
            return None
        cmd.usage_count += 1
        return cmd.render(variables)

    def list_commands(self, scope: str = None) -> List[Dict]:
        cmds = self._commands.values()
        if scope:
            cmds = [c for c in cmds if c.scope == scope]
        return [
            {
                "name": c.name,
                "description": c.description,
                "variables": c.variables,
                "scope": c.scope,
                "usage_count": c.usage_count,
            }
            for c in sorted(cmds, key=lambda c: c.name)
        ]

    def delete_command(self, name: str) -> bool:
        cmd = self._commands.get(name)
        if not cmd:
            return False
        del self._commands[name]
        target_dir = self.user_dir if cmd.scope == "user" else self.project_dir
        file_path = target_dir / f"{name}.md"
        if file_path.exists():
            file_path.unlink()
        return True

    def get_stats(self) -> Dict:
        user_cmds = sum(1 for c in self._commands.values() if c.scope == "user")
        project_cmds = sum(1 for c in self._commands.values() if c.scope == "project")
        total_usage = sum(c.usage_count for c in self._commands.values())
        return {
            "total_commands": len(self._commands),
            "user_commands": user_cmds,
            "project_commands": project_cmds,
            "total_usage": total_usage,
        }
