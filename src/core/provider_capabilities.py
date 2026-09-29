from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderCapabilities:
    native_tool_calling: bool = False
    vision: bool = False

    @classmethod
    def full(cls) -> "ProviderCapabilities":
        return cls(native_tool_calling=True, vision=True)

    @classmethod
    def text_only(cls) -> "ProviderCapabilities":
        return cls(native_tool_calling=False, vision=False)

    @classmethod
    def tools_only(cls) -> "ProviderCapabilities":
        return cls(native_tool_calling=True, vision=False)


class ToolsPayloadKind:
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    PROMPT_GUIDED = "prompt_guided"


@dataclass
class ToolsPayload:
    kind: str
    payload: dict
    instructions: str = ""

    @classmethod
    def openai(cls, tools: list[dict]) -> "ToolsPayload":
        return cls(kind=ToolsPayloadKind.OPENAI, payload={"tools": tools})

    @classmethod
    def anthropic(cls, tools: list[dict]) -> "ToolsPayload":
        return cls(kind=ToolsPayloadKind.ANTHROPIC, payload={"tools": tools})

    @classmethod
    def gemini(cls, declarations: list[dict]) -> "ToolsPayload":
        return cls(kind=ToolsPayloadKind.GEMINI, payload={"function_declarations": declarations})

    @classmethod
    def prompt_guided(cls, instructions: str) -> "ToolsPayload":
        return cls(kind=ToolsPayloadKind.PROMPT_GUIDED, payload={}, instructions=instructions)


@dataclass
class ModelInfo:
    id: str
    name: str
    context_window: int
    provider: str
    supports_vision: bool = False
    supports_tools: bool = False
    pricing_input_per_mtok: float = 0.0
    pricing_output_per_mtok: float = 0.0


MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "gpt-4-turbo": 128000,
    "claude-3-5-sonnet-20241022": 200000,
    "claude-3-opus-20240229": 200000,
    "claude-3-haiku-20240307": 200000,
    "claude-sonnet-4": 200000,
    "gemini/gemini-2.0-flash": 1048576,
    "gemini/gemini-2.0-pro": 2097152,
    "gemma4:latest": 128000,
    "deepseek-r1:8b": 32000,
    "qwen2.5-coder:7b": 32000,
    "qwen2.5:0.5b": 32000,
    "qwen2.5vl:7b": 32000,
    "nemotron-3-nano:4b": 32000,
    "moondream:latest": 32000,
    "nexus-director-v5:latest": 32000,
    "nexus-director-v6:latest": 32000,
    "nomic-embed-text:latest": 8192,
    "deepseek-v4-flash-free": 128000,
    "deepseek-v4-flash": 128000,
    "deepseek-v4-pro": 128000,
    "kimi-k2.5": 128000,
    "kimi-k2.6": 128000,
    "glm-5": 128000,
    "glm-5.1": 128000,
}


def context_window_for_model(model: str) -> int:
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]
    for prefix, window in sorted(MODEL_CONTEXT_WINDOWS.items(), key=lambda x: -len(x[0])):
        if model.startswith(prefix):
            return window
    return 128000
