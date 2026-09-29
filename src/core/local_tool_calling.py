"""
Local Tool Calling - Function calling nativo via qwen2.5-coder <tool_call> format.

Permite que modelos locales (qwen2.5-coder) invoquen herramientas NEXUS
sin depender de APIs cloud. Parsea el formato <tool_call> nativo del modelo.

Formato qwen2.5-coder:
    <tool_call>
    {"name": "function_name", "arguments": {"key": "value"}}
    </tool_call>

Uso:
    caller = LocalToolCaller(ollama_client, tool_registry)
    result = await caller.run("Lee el archivo config.py y dime qué hace")
"""

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Awaitable, Dict, List, Optional

logger = logging.getLogger(__name__)

TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


@dataclass
class ToolDefinition:
    """Definicion de una herramienta invocable por LLM local."""
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Optional[Callable[..., Awaitable[Any]]] = None

    def to_schema(self) -> Dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class LocalToolCaller:
    """
    Ejecuta tool calling local con modelos Ollama que soportan <tool_call>.

    Loop: prompt -> LLM -> parse <tool_call> -> execute -> feed result -> repeat
    Max 5 iterations para evitar loops infinitos.
    """

    def __init__(
        self,
        ollama_client,
        tools: Optional[List[ToolDefinition]] = None,
        model: str = "nexus-coder",
        max_iterations: int = 5,
    ):
        self.ollama = ollama_client
        self.model = model
        self.max_iterations = max_iterations
        self._tools: Dict[str, ToolDefinition] = {}
        if tools:
            for t in tools:
                self.register_tool(t)

    def register_tool(self, tool: ToolDefinition):
        self._tools[tool.name] = tool

    def register_handler(self, name: str, description: str, parameters: Dict, handler: Callable):
        """Shorthand para registrar tool con handler."""
        self._tools[name] = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            handler=handler,
        )

    def _build_system_prompt(self) -> str:
        tools_json = json.dumps(
            [t.to_schema() for t in self._tools.values()],
            indent=2,
        )
        return f"""You are NEXUS Coder with tool calling capabilities.
You have access to the following tools:

{tools_json}

When you need to use a tool, respond with:
<tool_call>
{{"name": "tool_name", "arguments": {{"param": "value"}}}}
</tool_call>

Rules:
- Use tools when the task requires file access, execution, or external data
- You can call multiple tools sequentially (one per response)
- After receiving tool results, provide your final answer
- If no tools are needed, respond directly
- Always respond in the same language as the user's request"""

    def parse_tool_calls(self, text: str) -> List[Dict]:
        """Extrae tool calls del output del LLM."""
        calls = []
        for match in TOOL_CALL_PATTERN.finditer(text):
            try:
                data = json.loads(match.group(1))
                if "name" in data:
                    calls.append({
                        "name": data["name"],
                        "arguments": data.get("arguments", {}),
                    })
            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse tool_call JSON: {e}")
        return calls

    async def execute_tool(self, name: str, arguments: Dict) -> str:
        """Ejecuta una tool registrada y devuelve resultado como string."""
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Tool '{name}' not found. Available: {list(self._tools.keys())}"
        if not tool.handler:
            return f"Error: Tool '{name}' has no handler registered"

        try:
            result = await tool.handler(**arguments)
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False, default=str)[:4000]
            return str(result)[:4000]
        except Exception as e:
            logger.error(f"Tool {name} execution error: {e}")
            return f"Error executing {name}: {e}"

    async def run(
        self,
        user_message: str,
        context: str = "",
        conversation: Optional[List[Dict]] = None,
    ) -> Dict:
        """
        Ejecuta un ciclo completo de tool calling.

        Returns:
            {"content": str, "tool_calls": [...], "iterations": int}
        """
        messages = [{"role": "system", "content": self._build_system_prompt()}]

        if conversation:
            messages.extend(conversation)

        prompt = user_message
        if context:
            prompt = f"Context:\n{context}\n\nTask: {user_message}"
        messages.append({"role": "user", "content": prompt})

        all_tool_calls = []
        iterations = 0

        for i in range(self.max_iterations):
            iterations = i + 1

            try:
                response = await self.ollama.chat(
                    model=self.model,
                    messages=messages,
                    options={"temperature": 0.3, "num_predict": 1000, "num_ctx": 4096},
                )
            except Exception as e:
                logger.error(f"Ollama chat error in tool calling loop: {e}")
                return {"content": f"Error: {e}", "tool_calls": all_tool_calls, "iterations": iterations}

            content = response.get("message", {}).get("content", "")
            calls = self.parse_tool_calls(content)

            if not calls:
                # No tool calls — LLM is done, return final answer
                # Strip any remaining tags
                clean = re.sub(r"</?tool_call>", "", content).strip()
                return {"content": clean, "tool_calls": all_tool_calls, "iterations": iterations}

            # Execute each tool call and feed results back
            messages.append({"role": "assistant", "content": content})

            for call in calls:
                all_tool_calls.append(call)
                result = await self.execute_tool(call["name"], call["arguments"])
                messages.append({
                    "role": "user",
                    "content": f"<tool_result name=\"{call['name']}\">\n{result}\n</tool_result>",
                })
                logger.info(f"Tool call: {call['name']}({call['arguments']}) -> {len(result)} chars")

        # Max iterations reached
        return {
            "content": "Max tool calling iterations reached",
            "tool_calls": all_tool_calls,
            "iterations": iterations,
        }

    def get_registered_tools(self) -> List[str]:
        return list(self._tools.keys())

    def get_tool_schemas(self) -> List[Dict]:
        return [t.to_schema() for t in self._tools.values()]
