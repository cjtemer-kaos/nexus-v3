import enum
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class PermissionLevel(enum.IntEnum):
    NONE = 0
    READ_ONLY = 1
    WRITE = 2
    EXECUTE = 3
    DANGEROUS = 4


class ToolScope(enum.Enum):
    ALL = "all"
    AGENT_ONLY = "agent_only"
    CLI_RPC_ONLY = "cli_rpc_only"


class ToolCategory(enum.Enum):
    SYSTEM = "system"
    WORKFLOW = "skill"


@dataclass
class ToolResult:
    content: str = ""
    markdown: Optional[str] = None
    is_error: bool = False
    metadata: dict = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, markdown: Optional[str] = None) -> "ToolResult":
        return cls(content=content, markdown=markdown)

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls(content=message, is_error=True)

    def to_json(self) -> str:
        return json.dumps({"content": self.content, "is_error": self.is_error, "metadata": self.metadata})


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    required: list = field(default_factory=list)

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }

    def to_anthropic_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.parameters,
                "required": self.required,
            },
        }


class ToolCallResult:
    def __init__(self, call_id: str, name: str, content: str):
        self.call_id = call_id
        self.name = name
        self.content = content


class ToolCallRequest:
    def __init__(self, call_id: str, name: str, arguments: dict, extra_content: Optional[dict] = None):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments
        self.extra_content = extra_content


class ToolCallOptions:
    def __init__(self, prefer_markdown: bool = False):
        self.prefer_markdown = prefer_markdown


class Tool(ABC):
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def description(self) -> str: ...

    @abstractmethod
    def parameters_schema(self) -> dict: ...

    @abstractmethod
    async def execute(self, args: dict) -> ToolResult: ...

    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    def permission_level_with_args(self, args: dict) -> PermissionLevel:
        return self.permission_level()

    def scope(self) -> ToolScope:
        return ToolScope.ALL

    def category(self) -> ToolCategory:
        return ToolCategory.SYSTEM

    def is_concurrency_safe(self, args: dict) -> bool:
        return False

    def external_effect(self) -> bool:
        return False

    def external_effect_with_args(self, args: dict) -> bool:
        return self.external_effect()

    def max_result_size_chars(self) -> Optional[int]:
        return None

    def supports_markdown(self) -> bool:
        return False

    async def execute_with_options(self, args: dict, options: ToolCallOptions) -> ToolResult:
        return await self.execute(args)

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name(),
            description=self.description(),
            parameters=self.parameters_schema(),
        )

    def required_args(self) -> list:
        return []


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._categories: dict[ToolCategory, list[str]] = {
            ToolCategory.SYSTEM: [],
            ToolCategory.WORKFLOW: [],
        }

    def register(self, tool: Tool) -> None:
        name = tool.name()
        self._tools[name] = tool
        self._categories[tool.category()].append(name)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def get_by_category(self, category: ToolCategory) -> list[Tool]:
        return [self._tools[n] for n in self._categories.get(category, []) if n in self._tools]

    def get_specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def count(self) -> int:
        return len(self._tools)
