import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class MCPServerInfo:
    name: str
    command: str
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    installed: bool = False
    bin_path: Optional[str] = None

    def to_mcp_json(self) -> dict:
        config: dict = {"command": self.command}
        if self.args:
            config["args"] = self.args
        if self.env:
            config["env"] = {k: f"${{{k}}}" for k in self.env}
        return config


@dataclass
class MCPRegistry:
    servers: dict[str, MCPServerInfo] = field(default_factory=dict)

    def register(self, server: MCPServerInfo) -> None:
        self.servers[server.name] = server

    def get(self, name: str) -> Optional[MCPServerInfo]:
        return self.servers.get(name)

    def list_names(self) -> list[str]:
        return list(self.servers.keys())

    def installable(self) -> list[str]:
        return [n for n, s in self.servers.items() if not s.installed]

    def installed(self) -> list[str]:
        return [n for n, s in self.servers.items() if s.installed]

    def to_claude_config(self) -> dict:
        return {
            "mcpServers": {
                name: svr.to_mcp_json() for name, svr in self.servers.items()
            }
        }


class MCPRegistryClient:
    DEFAULT_SOURCES = {
        "playwright": MCPServerInfo(
            name="playwright", command="npx", args=["@playwright/mcp"],
            installed=True,
        ),
        "github": MCPServerInfo(
            name="github", command="npx", args=["-y", "@modelcontextprotocol/server-github"],
            installed=True,
        ),
        "brave-search": MCPServerInfo(
            name="brave-search", command="npx", args=["-y", "@anthropic-ai/brave-search-mcp"],
            installed=True,
        ),
        "context7": MCPServerInfo(
            name="context7", command="npx", args=["-y", "@upstash/context7-mcp"],
            installed=True,
        ),
        "chrome-devtools": MCPServerInfo(
            name="chrome-devtools", command="npx", args=["chrome-devtools-mcp"],
            installed=True,
        ),
        "filesystem": MCPServerInfo(
            name="filesystem", command="npx", args=["-y", "@modelcontextprotocol/server-filesystem"],
            installed=False,
        ),
        "sqlite": MCPServerInfo(
            name="sqlite", command="uvx", args=["sqlite-mcp"],
            installed=False,
        ),
        "memory": MCPServerInfo(
            name="memory", command="npx", args=["-y", "@modelcontextprotocol/server-memory"],
            installed=False,
        ),
        "sequential-thinking": MCPServerInfo(
            name="sequential-thinking", command="npx", args=["-y", "@modelcontextprotocol/server-sequential-thinking"],
            installed=False,
        ),
    }

    def __init__(self):
        self.registry = MCPRegistry()
        for name, info in self.DEFAULT_SOURCES.items():
            self.registry.register(info)

    def list_servers(self, include_uninstallable: bool = False) -> list[MCPServerInfo]:
        return list(self.registry.servers.values())

    def search(self, query: str) -> list[MCPServerInfo]:
        q = query.lower()
        return [
            s for s in self.registry.servers.values()
            if q in s.name.lower() or q in (s.command or "").lower()
        ]

    def register_custom(self, name: str, command: str, args: Optional[list] = None,
                        env: Optional[dict] = None) -> MCPServerInfo:
        info = MCPServerInfo(name=name, command=command, args=args or [], env=env or {},
                             installed=shutil.which(command) is not None)
        self.registry.register(info)
        return info

    def is_installed(self, name: str) -> bool:
        info = self.registry.get(name)
        if not info:
            return False
        if info.installed:
            return True
        return shutil.which(info.command) is not None

    def install(self, name: str) -> bool:
        info = self.registry.get(name)
        if not info:
            logger.error("Unknown MCP server: %s", name)
            return False
        if self.is_installed(name):
            info.installed = True
            return True
        cmd = [info.command] + info.args + ["--install"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                info.installed = True
                return True
            logger.error("Install failed for %s: %s", name, result.stderr)
            return False
        except Exception as e:
            logger.error("Install exception for %s: %s", name, e)
            return False
