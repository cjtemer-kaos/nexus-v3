"""
Agent Communication - Flujos entre agentes para SuperNEXUS v2.0

SendMessage + Handoff patterns de OpenSwarm.
Comunicacion any-to-any entre gemas.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from src.core.event_bus import EventBus, Message, EventType, Handoff

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class AgentCapability:
    """Capacidad de un agente"""
    name: str
    description: str
    tags: List[str]
    can_handle: List[str]  # Tipos de tareas que puede manejar


@dataclass
class AgentRegistry:
    """Registro de agentes disponibles"""
    agents: Dict[str, AgentCapability] = field(default_factory=dict)

    def register(self, name: str, capability: AgentCapability):
        """Registra un agente"""
        self.agents[name] = capability

    def find_for_task(self, task_type: str) -> List[str]:
        """Encuentra agentes que pueden manejar un tipo de tarea"""
        return [
            name for name, cap in self.agents.items()
            if task_type in cap.can_handle or any(tag == task_type for tag in cap.tags)
        ]

    def get_all(self) -> Dict[str, AgentCapability]:
        """Obtiene todos los agentes"""
        return self.agents


class CommunicationFlow:
    """
    Gestion de comunicacion entre agentes.
    Patterns: SendMessage (corto), Handoff (transferencia completa).
    """

    def __init__(self, event_bus: Optional[EventBus] = None):
        self.event_bus = event_bus or EventBus()
        self.registry = AgentRegistry()
        self._handlers: Dict[str, Callable] = {}
        self._active_flows: Dict[str, Dict] = {}

    def register_agent(self, name: str, capability: AgentCapability, handler: Callable):
        """Registra un agente con su handler"""
        self.registry.register(name, capability)
        self._handlers[name] = handler

    async def send_message(
        self,
        from_agent: str,
        to_agent: str,
        content: str,
        context: Optional[Dict] = None,
    ) -> Dict:
        """
        SendMessage: comunicacion corta entre agentes.
        El agente origen mantiene el control.
        """
        message = Message(
            source=from_agent,
            target=to_agent,
            event_type=EventType.MESSAGE,
            content=content,
            metadata=context or {},
        )

        await self.event_bus.publish(message)

        # Ejecutar handler del agente destino
        handler = self._handlers.get(to_agent)
        if handler:
            try:
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(message)
                else:
                    result = handler(message)

                response = Message(
                    source=to_agent,
                    target=from_agent,
                    event_type=EventType.MESSAGE,
                    content=str(result),
                    parent_id=message.id,
                )
                await self.event_bus.publish(response)
                return {"success": True, "response": result}
            except Exception as e:
                logger.error(f"Handler error for {to_agent}: {e}")
                return {"success": False, "error": str(e)}

        return {"success": False, "error": f"No handler for {to_agent}"}

    async def handoff(
        self,
        from_agent: str,
        to_agent: str,
        task: str,
        context: Optional[Dict] = None,
    ) -> Dict:
        """
        Handoff: transferencia completa de tarea a otro agente.
        El agente destino toma el control.
        """
        handoff = Handoff(
            from_agent=from_agent,
            to_agent=to_agent,
            task=task,
            context=context or {},
        )

        message = Message(
            source=from_agent,
            target=to_agent,
            event_type=EventType.HANDOFF,
            content=task,
            metadata={"handoff_id": handoff.id, "context": handoff.context},
        )

        await self.event_bus.publish(message)

        # Registrar flow activo
        self._active_flows[handoff.id] = {
            "handoff": handoff,
            "status": "transferred",
            "started_at": datetime.now().isoformat(),
        }

        # Ejecutar handler del agente destino
        handler = self._handlers.get(to_agent)
        if handler:
            try:
                if asyncio.iscoroutinefunction(handler):
                    result = await handler(message)
                else:
                    result = handler(message)

                self._active_flows[handoff.id]["status"] = "completed"
                self._active_flows[handoff.id]["result"] = str(result)

                await self.event_bus.publish(Message(
                    source=to_agent,
                    target=from_agent,
                    event_type=EventType.TASK_COMPLETE,
                    content=str(result),
                    metadata={"handoff_id": handoff.id},
                ))

                return {"success": True, "handoff_id": handoff.id, "result": result}
            except Exception as e:
                self._active_flows[handoff.id]["status"] = "failed"
                self._active_flows[handoff.id]["error"] = str(e)

                await self.event_bus.publish(Message(
                    source=to_agent,
                    target=from_agent,
                    event_type=EventType.TASK_FAILED,
                    content=str(e),
                    metadata={"handoff_id": handoff.id},
                ))

                return {"success": False, "handoff_id": handoff.id, "error": str(e)}

        return {"success": False, "error": f"No handler for {to_agent}"}

    async def swarm_execute(
        self,
        task: str,
        agents: List[str],
        parallel: bool = True,
    ) -> Dict:
        """
        Ejecucion en swarm: multiple agentes trabajan en la tarea.
        """
        results = {}

        if parallel:
            # Ejecucion paralela
            tasks = []
            for agent in agents:
                handler = self._handlers.get(agent)
                if handler:
                    message = Message(
                        source="swarm",
                        target=agent,
                        event_type=EventType.MESSAGE,
                        content=task,
                    )
                    if asyncio.iscoroutinefunction(handler):
                        tasks.append(handler(message))
                    else:
                        tasks.append(asyncio.to_thread(handler, message))

            outputs = await asyncio.gather(*tasks, return_exceptions=True)
            for agent, output in zip(agents, outputs):
                if isinstance(output, Exception):
                    results[agent] = {"success": False, "error": str(output)}
                else:
                    results[agent] = {"success": True, "result": output}
        else:
            # Ejecucion secuencial con handoffs
            current_result = task
            for i, agent in enumerate(agents):
                handler = self._handlers.get(agent)
                if handler:
                    message = Message(
                        source=agents[i - 1] if i > 0 else "swarm",
                        target=agent,
                        event_type=EventType.MESSAGE,
                        content=current_result,
                    )
                    if asyncio.iscoroutinefunction(handler):
                        current_result = await handler(message)
                    else:
                        current_result = handler(message)
                    results[agent] = {"success": True, "result": current_result}

        return {"agents": agents, "results": results}

    def get_active_flows(self) -> List[Dict]:
        """Obtiene flujos activos"""
        return [
            f for f in self._active_flows.values()
            if f.get("status") in ("transferred", "running")
        ]

    def get_stats(self) -> Dict:
        """Estadisticas de comunicacion"""
        return {
            "registered_agents": len(self.registry.agents),
            "active_flows": len(self.get_active_flows()),
            "total_flows": len(self._active_flows),
            "agents": list(self.registry.agents.keys()),
        }
