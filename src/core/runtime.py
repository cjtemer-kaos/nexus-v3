"""
Agent Runtime Loop - Rowboat pattern para SuperNEXUS v2.0

Loop de ejecucion de agentes con tool permissions, event-sourced state,
y ciclo de pensamiento-accion-observacion.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from src.core.event_bus import EventBus, Message, EventType
from src.core.tool_security import is_public_blocked_tool
from src.tools.builtin import WorkspaceTools, ExecuteTools, ParseTools

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class AgentState(Enum):
    """Estados del agente"""
    IDLE = "idle"
    THINKING = "thinking"
    ACTING = "acting"
    WAITING = "waiting"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class ToolCall:
    """Llamada a herramienta"""
    name: str
    arguments: Dict[str, Any]
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0


@dataclass
class AgentTurn:
    """Un turno del agente (pensamiento-accion-observacion)"""
    turn_number: int
    thought: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    observation: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class AgentSession:
    """Sesion completa de un agente"""
    id: str
    agent_name: str
    task: str
    state: AgentState = AgentState.IDLE
    turns: List[AgentTurn] = field(default_factory=list)
    final_result: str = ""
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    max_turns: int = 20


class ToolPermission:
    """Permisos de herramientas"""

    def __init__(self):
        self.allowed_tools: Dict[str, List[str]] = {
            "default": ["read_file", "list_dir", "parse_file"],
            "coder": ["read_file", "write_file", "list_dir", "execute_command", "parse_file"],
            "debugger": ["read_file", "list_dir", "execute_command", "parse_file"],
            "devops": ["read_file", "write_file", "list_dir", "execute_command"],
            "scholar": ["read_file", "write_file"],
        }

    def is_allowed(self, agent: str, tool: str) -> bool:
        """Verifica si un agente puede usar una herramienta.

        Capa 1: tool_security — non-admin blocked tools (bash, write_file, vault, etc.)
        Capa 2: role-based permissions — specific tool allowlists per agent.
        """
        if is_public_blocked_tool(tool):
            logger.warning(f"Tool {tool} blocked by security policy for agent {agent}")
            return False
        allowed = self.allowed_tools.get(agent, self.allowed_tools["default"])
        return tool in allowed


class AgentRuntime:
    """
    Runtime de agentes con loop de ejecucion.
    Pattern: Rowboat agent loop con event-sourced state.
    """

    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus()
        self.permissions = ToolPermission()
        self.workspace = WorkspaceTools()
        self.executor = ExecuteTools()
        self.parser = ParseTools()
        self._sessions: Dict[str, AgentSession] = {}
        self._tools = {
            "read_file": self.workspace.read_file,
            "write_file": self.workspace.write_file,
            "list_dir": self.workspace.list_dir,
            "execute_command": self.executor.execute_command,
            "parse_file": self.parser.parse_file,
        }

    async def run_task(self, session_id: str, agent_name: str, task: str, max_turns: int = 20) -> AgentSession:
        """
        Ejecuta tarea con loop de agente.
        Ciclo: pensar -> actuar -> observar -> repetir
        """
        session = AgentSession(
            id=session_id,
            agent_name=agent_name,
            task=task,
            max_turns=max_turns,
            state=AgentState.THINKING,
        )
        self._sessions[session_id] = session

        await self._emit_event(EventType.SYSTEM, f"Agent {agent_name} starting task: {task[:100]}")

        for turn_num in range(1, max_turns + 1):
            session.state = AgentState.THINKING

            # 1. THINK: Generar pensamiento y plan
            thought = await self._think(session, turn_num)
            turn = AgentTurn(turn_number=turn_num, thought=thought)
            session.turns.append(turn)

            # 2. ACT: Ejecutar herramientas
            session.state = AgentState.ACTING
            tool_calls = await self._act(session, turn, agent_name)
            turn.tool_calls = tool_calls

            # 3. OBSERVE: Procesar resultados
            session.state = AgentState.WAITING
            observation = await self._observe(session, turn)
            turn.observation = observation

            # 4. Verificar si completo
            if await self._is_complete(session, turn):
                session.state = AgentState.COMPLETE
                session.completed_at = datetime.now().isoformat()
                await self._emit_event(EventType.TASK_COMPLETE, f"Task completed in {turn_num} turns")
                break

            # Emitir evento de turno completado
            await self._emit_event(EventType.MESSAGE, f"Turn {turn_num} completed")

        if session.state != AgentState.COMPLETE:
            session.state = AgentState.ERROR
            session.final_result = "Max turns reached without completion"
            await self._emit_event(EventType.TASK_FAILED, "Max turns reached")

        return session

    async def _think(self, session: AgentSession, turn_num: int) -> str:
        """Fase de pensamiento"""
        # En produccion: llamar a LLM para generar pensamiento
        return f"Turn {turn_num}: Analyzing task progress..."

    async def _act(self, session: AgentSession, turn: AgentTurn, agent_name: str) -> List[ToolCall]:
        """Fase de accion - ejecutar herramientas"""
        # En produccion: LLM decide que herramientas llamar
        # Por ahora: ejemplo con lectura de archivo
        tool_calls = []

        # Simular llamada a herramienta
        tool_name = "read_file"
        if self.permissions.is_allowed(agent_name, tool_name):
            start = time.time()
            try:
                result = self._tools[tool_name]("config/settings.json")
                duration = (time.time() - start) * 1000
                tool_calls.append(ToolCall(
                    name=tool_name,
                    arguments={"path": "config/settings.json"},
                    result=result,
                    duration_ms=duration,
                ))
            except Exception as e:
                duration = (time.time() - start) * 1000
                tool_calls.append(ToolCall(
                    name=tool_name,
                    arguments={"path": "config/settings.json"},
                    error=str(e),
                    duration_ms=duration,
                ))

        return tool_calls

    async def _observe(self, session: AgentSession, turn: AgentTurn) -> str:
        """Fase de observacion"""
        observations = []
        for tc in turn.tool_calls:
            if tc.error:
                observations.append(f"Error calling {tc.name}: {tc.error}")
            else:
                observations.append(f"{tc.name} completed in {tc.duration_ms:.0f}ms")
        return "\n".join(observations) if observations else "No actions taken"

    async def _is_complete(self, session: AgentSession, turn: AgentTurn) -> bool:
        """Verificar si la tarea esta completa"""
        # En produccion: LLM decide si la tarea esta completa
        return False

    async def _emit_event(self, event_type: EventType, content: str):
        """Emite evento al bus"""
        message = Message(
            source="runtime",
            target="*",
            event_type=event_type,
            content=content,
        )
        await self.event_bus.publish(message)

    def get_session(self, session_id: str) -> Optional[AgentSession]:
        """Obtiene sesion por ID"""
        return self._sessions.get(session_id)

    def get_active_sessions(self) -> List[AgentSession]:
        """Obtiene sesiones activas"""
        return [
            s for s in self._sessions.values()
            if s.state not in (AgentState.COMPLETE, AgentState.ERROR)
        ]

    def get_stats(self) -> Dict:
        """Estadisticas del runtime"""
        by_state = {}
        for s in self._sessions.values():
            state = s.state.value
            by_state[state] = by_state.get(state, 0) + 1

        return {
            "total_sessions": len(self._sessions),
            "by_state": by_state,
            "tools_available": list(self._tools.keys()),
        }
