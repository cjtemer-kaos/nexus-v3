"""
Agent Teams — coordinación multi-agente con message board persistente + RealtimeHub

MessageBoard: buzón SQLite persistente para comunicación asíncrona entre agentes
AgentTeamManager: orquestador que lee del board y delega tareas a workers
ParallelTeamExecutor: ejecutor paralelo de gemas con agregación de resultados

Diferenciación:
  - MessageBoard (SQLite, polling) → persistencia, cross-session
  - src.core.message_bus.MessageBus (in-memory, async) → intra-sesión, pub/sub
  - RealtimeHub (scopes + dedup + Redis) → eventos en tiempo real multi-nodo
"""

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.core.db_wal import db_connection

logger = logging.getLogger("nexus-teams")


@dataclass
class Message:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    sender: str = ""
    target: str = ""
    msg_type: str = "request"
    content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    in_reply_to: str = ""
    status: str = "pending"


@dataclass
class AgentTeam:
    name: str
    members: List[str] = field(default_factory=list)
    description: str = ""


class MessageBoard:
    """
    Buzón de mensajes persistente SQLite para comunicación asíncrona entre agentes.
    Almacenamiento cross-session: los mensajes sobreviven a reinicios.

    Si hay un RealtimeHub, notifica eventos en tiempo real además de escribir a DB.
    """

    def __init__(self, db_path: str = None, hub=None):
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "message_bus.db")
        self.db_path = db_path
        self.hub = hub
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with db_connection(self.db_path, db_label="teams") as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                target TEXT NOT NULL,
                msg_type TEXT DEFAULT 'request',
                content TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}',
                timestamp REAL DEFAULT (strftime('%s', 'now')),
                in_reply_to TEXT DEFAULT '',
                status TEXT DEFAULT 'pending'
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS teams (
                name TEXT PRIMARY KEY,
                members TEXT DEFAULT '[]',
                description TEXT DEFAULT ''
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(target)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status)")

    def send(self, msg: Message) -> str:
        with db_connection(self.db_path, db_label="teams") as conn:
            conn.execute(
                "INSERT OR REPLACE INTO messages (id, sender, target, msg_type, content, metadata, timestamp, in_reply_to, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (msg.id, msg.sender, msg.target, msg.msg_type, msg.content,
                 json.dumps(msg.metadata), msg.timestamp, msg.in_reply_to, msg.status))

        if self.hub:
            asyncio.create_task(self.hub.publish(
                event_type=f"message.{msg.msg_type}",
                content=msg.content,
                scope=f"agent:{msg.target}",
                source=msg.sender,
                metadata={"message_id": msg.id, "in_reply_to": msg.in_reply_to},
            ))

        return msg.id

    def read(self, target: str, limit: int = 20, since: float = 0) -> List[Message]:
        messages = []
        with db_connection(self.db_path, db_label="teams") as conn:
            if since > 0:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE target = ? AND timestamp > ? ORDER BY timestamp DESC LIMIT ?",
                    (target, since, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE target = ? ORDER BY timestamp DESC LIMIT ?",
                    (target, limit)).fetchall()
            for r in rows:
                messages.append(Message(
                    id=r["id"], sender=r["sender"], target=r["target"],
                    msg_type=r["msg_type"], content=r["content"],
                    metadata=json.loads(r["metadata"]),
                    timestamp=r["timestamp"], in_reply_to=r["in_reply_to"],
                    status=r["status"]))
        return messages

    def mark_read(self, msg_id: str):
        with db_connection(self.db_path, db_label="teams") as conn:
            conn.execute("UPDATE messages SET status = 'read' WHERE id = ?", (msg_id,))

    def reply(self, original: Message, content: str, sender: str) -> str:
        reply_msg = Message(
            sender=sender,
            target=original.sender,
            msg_type="response",
            content=content,
            in_reply_to=original.id,
            metadata={"original_type": original.msg_type},
        )
        return self.send(reply_msg)

    def register_team(self, team: AgentTeam):
        with db_connection(self.db_path, db_label="teams") as conn:
            conn.execute("INSERT OR REPLACE INTO teams (name, members, description) VALUES (?, ?, ?)",
                         (team.name, json.dumps(team.members), team.description))

    def get_team(self, name: str) -> Optional[AgentTeam]:
        with db_connection(self.db_path, db_label="teams") as conn:
            row = conn.execute("SELECT * FROM teams WHERE name = ?", (name,)).fetchone()
            if row:
                return AgentTeam(name=row["name"], members=json.loads(row["members"]),
                                 description=row["description"])
        return None

    def list_teams(self) -> List[AgentTeam]:
        teams = []
        with db_connection(self.db_path, db_label="teams") as conn:
            for r in conn.execute("SELECT * FROM teams").fetchall():
                teams.append(AgentTeam(name=r["name"], members=json.loads(r["members"]),
                                       description=r["description"]))
        return teams

    def request(self, target: str, content: str, sender: str, timeout: float = 30.0) -> Optional[Message]:
        msg = Message(sender=sender, target=target, msg_type="request", content=content)
        self.send(msg)
        deadline = time.time() + timeout
        while time.time() < deadline:
            responses = self.read(target=msg.id, since=msg.timestamp)
            responses = [m for m in responses if m.in_reply_to == msg.id and m.status != "read"]
            if responses:
                return responses[0]
            time.sleep(0.5)
        return None

    def get_stats(self) -> Dict:
        with db_connection(self.db_path, db_label="teams") as conn:
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            pending = conn.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]
            teams = conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
            return {
                "total_messages": total,
                "pending_messages": pending,
                "registered_teams": teams,
                "db_path": self.db_path,
            }


class AgentTeamManager:
    def __init__(self, board: MessageBoard = None, agent_id: str = None, hub=None):
        self.board = board or MessageBoard(hub=hub)
        self.agent_id = agent_id or "agent-unknown"
        self._tasks: Dict[str, asyncio.Task] = {}

    async def listen_loop(self, handler: Callable, poll_interval: float = 1.0):
        last_poll = time.time()
        while True:
            try:
                messages = self.board.read(target=self.agent_id, since=last_poll)
                for msg in messages:
                    if msg.status == "pending":
                        self.board.mark_read(msg.id)
                        result = handler(msg)
                        if asyncio.iscoroutine(result):
                            await result
                last_poll = time.time()
            except Exception as e:
                logger.error(f"Listen loop error: {e}")
            await asyncio.sleep(poll_interval)

    async def claim_task(self, task_board: str = "tasks") -> Optional[Message]:
        pending = self.board.read(target=task_board, limit=10)
        for msg in pending:
            if msg.status == "pending" and msg.msg_type == "task":
                self.board.mark_read(msg.id)
                return msg
        return None

    def spawn_worker(self, name: str, coro):
        task = asyncio.create_task(coro)
        self._tasks[name] = task
        return task

    def cancel_worker(self, name: str):
        task = self._tasks.pop(name, None)
        if task:
            task.cancel()

    def get_workers_status(self) -> Dict:
        return {
            name: {
                "done": task.done(),
                "cancelled": task.cancelled() if task.done() else False,
            }
            for name, task in self._tasks.items()
        }


# ── Parallel Team Executor ───────────────────────────────────────────────────

@dataclass
class AgentResult:
    """Resultado de una gema en ejecución paralela."""
    gema: str
    success: bool
    content: str
    model: str = ""
    tokens_used: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SubTask:
    """Sub-tarea para descomposición DAG (Fix 4)."""
    id: str
    description: str
    gema: str
    depends_on: List[str] = field(default_factory=list)


@dataclass
class TeamExecution:
    """Estado de una ejecución paralela de equipo."""
    id: str
    task: str
    gemas: List[str]
    status: str = "running"  # running | done | error | partial
    results: Dict[str, AgentResult] = field(default_factory=dict)
    synthesis: Optional[str] = None
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    progress: Dict[str, str] = field(default_factory=dict)  # gema -> status


class ParallelTeamExecutor:
    """
    Ejecutor paralelo de gemas — despacha sub-tareas concurrentes a múltiples
    gemas y agrega resultados.

    Uso:
        executor = ParallelTeamExecutor(director)
        result = await executor.execute(
            task="Analizar el rendimiento del sistema",
            gemas=["code", "analyst", "security"],
            timeout=60.0,
            synthesize=True,
        )
    """

    # Sub-tasks predefinidas por gema — cómo adaptar la tarea general
    GEMA_PROMPTS = {
        "code": "Enfócate en el análisis de código, patrones, y posibles mejoras de implementación. Tarea: {task}",
        "scholar": "Investiga contexto, mejores prácticas, y documentación relevante. Tarea: {task}",
        "architect": "Analiza la arquitectura, diseño, y escalabilidad. Tarea: {task}",
        "analyst": "Analiza datos, métricas, y rendimiento cuantitativo. Tarea: {task}",
        "security": "Revisa vulnerabilidades, cumplimiento, y seguridad. Tarea: {task}",
        "debugger": "Identifica bugs, errores potenciales, y problemas de debug. Tarea: {task}",
        "optimizer": "Sugiere optimizaciones de rendimiento y eficiencia. Tarea: {task}",
        "tester": "Propone estrategias de testing, casos de prueba, y validación. Tarea: {task}",
        "devops": "Analiza infraestructura, despliegue, y operaciones. Tarea: {task}",
        "creative": "Aporta perspectivas creativas y contenido. Tarea: {task}",
        "sage": "Consulta memoria y conocimiento persistente relevante. Tarea: {task}",
        "design": "Analiza diseño UI/UX y multimedia. Tarea: {task}",
        "prompter": "Optimiza prompts y formatos de entrada para IA. Tarea: {task}",
        "engineer": "Analiza ingeniería de herramientas y automatización. Tarea: {task}",
        "biblioteca": "Organiza y estructura el conocimiento generado. Tarea: {task}",
    }

    def __init__(self, director=None, hub=None):
        self.director = director
        self.hub = hub
        self._executions: Dict[str, TeamExecution] = {}

    def _get_gema_prompt(self, gema: str, task: str) -> str:
        """Genera sub-tarea especializada para una gema."""
        template = self.GEMA_PROMPTS.get(gema, "Responde a esta tarea como experto en {gema}: {task}")
        return template.format(task=task, gema=gema)

    async def _run_single_gema(
        self,
        gema: str,
        task: str,
        execution: TeamExecution,
        timeout: float = 60.0,
        context: str = "",
    ) -> AgentResult:
        """Ejecuta una gema individual con timeout."""
        execution.progress[gema] = "running"
        self._notify_progress(execution, gema, "running")

        start = time.time()
        try:
            sub_task = self._get_gema_prompt(gema, task)

            if self.director:
                # Fix 1: Usar AgentRunner con tool whitelist (no director.execute sin tools)
                agent_result = await self._run_gema_agentic(gema, sub_task, context, timeout, start)

                # Fix 2.5: Verificador iterativo con threshold 80 y rebote
                if agent_result.success and agent_result.content.strip():
                    confidence, reason = await self._verify_result(task, gema, agent_result)
                    agent_result.metadata["confidence"] = confidence
                    agent_result.metadata["verifier_reason"] = reason
                    retries = 0
                    while confidence < 80.0 and retries < 2:
                        retries += 1
                        logger.warning(
                            f"[team] Gema {gema} confianza {confidence:.0f} < 80, "
                            f"reintento {retries}/2: {reason[:120]}"
                        )
                        bounce_ctx = f"{context}\n[Feedback del verificador]\n{reason}" if context else f"[Feedback del verificador]\n{reason}"
                        agent_result = await self._run_gema_agentic(gema, sub_task, bounce_ctx, timeout, start)
                        if agent_result.success and agent_result.content.strip():
                            confidence, reason = await self._verify_result(task, gema, agent_result)
                            agent_result.metadata["confidence"] = confidence
                            agent_result.metadata["verifier_reason"] = reason
                    if confidence < 80.0:
                        agent_result.success = False
                        agent_result.error = agent_result.error or f"low_confidence:{confidence:.0f}:{reason[:100]}"
                        agent_result.content = "(respuesta con baja confianza tras reintentos)"
            else:
                agent_result = await self._run_without_director(gema, sub_task, timeout)
                if agent_result.success:
                    agent_result.duration_ms = (time.time() - start) * 1000

            execution.progress[gema] = "done" if agent_result.success else "failed"
            execution.results[gema] = agent_result
            self._notify_progress(execution, gema, execution.progress[gema])
            return agent_result

        except asyncio.TimeoutError:
            duration = (time.time() - start) * 1000
            agent_result = AgentResult(
                gema=gema, success=False, content="",
                error=f"Timeout después de {timeout}s", duration_ms=duration,
            )
            execution.progress[gema] = "timeout"
            execution.results[gema] = agent_result
            self._notify_progress(execution, gema, "timeout")
            return agent_result

        except Exception as e:
            duration = (time.time() - start) * 1000
            agent_result = AgentResult(
                gema=gema, success=False, content="",
                error=str(e), duration_ms=duration,
            )
            execution.progress[gema] = "error"
            execution.results[gema] = agent_result
            self._notify_progress(execution, gema, "error")
            return agent_result

    async def _run_without_director(self, gema: str, task: str, timeout: float) -> AgentResult:
        """Fallback: ejecuta gema vía LLM directo sin Director."""
        try:
            import aiohttp
            payload = {
                "model": "qwen2.5-coder:7b",
                "messages": [
                    {"role": "system", "content": f"Eres una IA especialista en {gema}. Responde de forma concisa y técnica."},
                    {"role": "user", "content": task},
                ],
                "stream": False,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "http://localhost:11434/api/chat",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    data = await resp.json()
                    content = data.get("message", {}).get("content", "")
                    return AgentResult(gema=gema, success=True, content=content)
        except Exception as e:
            return AgentResult(gema=gema, success=False, content="", error=str(e))

    def _build_gema_system_prompt(self, gema: str) -> str:
        """Fix 1.5: System prompt específico por gema, no el prompt genérico del router."""
        manifest = self.director.gemas.get(gema)
        model = manifest.model if manifest and hasattr(manifest, "model") else "unknown"
        desc = manifest.description if manifest and hasattr(manifest, "description") else ""
        caps = getattr(manifest, "capabilities", []) or []
        caps_str = ", ".join(caps[:5]) if caps else ""
        parts = [f"Eres {gema}, un agente especialista en el ecosistema SuperNEXUS."]
        parts.append(f"Modelo: {model}")
        if desc:
            parts.append(f"Rol: {desc}")
        if caps_str:
            parts.append(f"Capacidades: {caps_str}")
        parts.append(
            "Tienes acceso a herramientas para ejecutar comandos, leer/escribir archivos, "
            "y realizar análisis. Usa las herramientas cuando sea necesario. "
            "Responde de forma técnica y concisa en español."
        )
        return "\n".join(parts)

    async def _run_gema_agentic(
        self,
        gema: str,
        task: str,
        context: str,
        timeout: float,
        start: float,
    ) -> AgentResult:
        """Fix 1: Ejecuta gema con AgentRunner (tool loop real) + tool whitelist por gema.
        Fallback a director.execute() si AgentRunner falla.
        Fix 1.5: Usa _build_gema_system_prompt() en vez de _build_director_system_prompt().
        """
        from src.core.agent_runner import AgentRunner, AgentRunSpec
        from src.core.provider_base import LLMMessage
        from src.services.execution_service import ExecutionService

        try:
            provider = self.director.provider_registry.get("gema-con-fallback")
            if provider is None:
                raise RuntimeError("No provider available")

            tool_schemas = ExecutionService.tool_schemas_for_gem(self.director, gema)
            system_prompt = self._build_gema_system_prompt(gema)
            full_task = f"Context:\n{context}\n\nTask: {task}" if context else task

            memory_ctx = self.director._get_memory_context(full_task)
            if memory_ctx:
                full_task = f"[Memorias relevantes]\n{memory_ctx}\n\n---\n\n{full_task}"

            messages = [LLMMessage(role="system", content=system_prompt)]
            messages.append(LLMMessage(role="user", content=full_task))

            # Fix 6: Permission gating en el tool executor
            tool_executor = self._permission_gated_executor if self.hub else self.director._multi_motor_tool_executor
            runner = AgentRunner(provider, tool_executor=tool_executor)
            spec = AgentRunSpec(messages=messages, tools_definitions=tool_schemas, max_iterations=8)
            runner_result = await asyncio.wait_for(runner.run(spec), timeout=timeout)

            duration = (time.time() - start) * 1000

            if runner_result.stop_reason in ("error", "empty_final_response"):
                return AgentResult(
                    gema=gema, success=False, content=runner_result.content or "",
                    error=runner_result.error, duration_ms=duration,
                )

            content = runner_result.content or ""
            if not content.strip():
                return AgentResult(
                    gema=gema, success=False, content="(sin respuesta)",
                    error="AgentRunner returned empty content", duration_ms=duration,
                )

            total_tokens = runner_result.usage.get("prompt_tokens", 0) + runner_result.usage.get("completion_tokens", 0)
            return AgentResult(
                gema=gema, success=True, content=content,
                model=provider.model, tokens_used=total_tokens, duration_ms=duration,
            )

        except asyncio.TimeoutError:
            duration = (time.time() - start) * 1000
            return AgentResult(
                gema=gema, success=False, content="",
                error=f"Timeout after {timeout}s", duration_ms=duration,
            )

        except Exception as e:
            duration = (time.time() - start) * 1000
            logger.warning(f"[team] AgentRunner failed for {gema}, falling back to director.execute: {e}")
            try:
                result = await asyncio.wait_for(
                    self.director.execute(task, gem=gema, context=context),
                    timeout=timeout,
                )
                duration = (time.time() - start) * 1000
                success = getattr(result, 'success', False)
                content = ""
                if hasattr(result, 'data') and isinstance(result.data, dict):
                    content = result.data.get('content', '')
                elif hasattr(result, 'content'):
                    content = result.content
                if not content or not content.strip():
                    success = False
                    content = "(sin respuesta)"
                return AgentResult(
                    gema=gema, success=success, content=content,
                    model=self.director.gemas.get(gema, None) and self.director.gemas[gema].model or "",
                    tokens_used=result.data.get('tokens_used', 0) if hasattr(result, 'data') and isinstance(result.data, dict) else 0,
                    duration_ms=duration,
                )
            except Exception as e2:
                return AgentResult(
                    gema=gema, success=False, content="",
                    error=f"AgentRunner+fallback: {e}; {e2}", duration_ms=(time.time() - start) * 1000,
                )

    async def _verify_result(
        self,
        task: str,
        gema: str,
        result: AgentResult,
        timeout: float = 15.0,
    ) -> tuple[float, str]:
        """Fix 2.5: Verificador iterativo (port odysseus agent_loop.py:1175).
        Evalúa el resultado con un LLM call independiente (sin historia compartida)
        y retorna (confidence 0-100, reason). Threshold objetivo: 80.
        """
        if not result.success or not result.content.strip():
            return (0.0, "resultado vacio o fallido")

        from src.core.provider_base import LLMMessage

        try:
            provider = self.director.provider_registry.get("gema-con-fallback")
            if provider is None:
                return (50.0, "no provider disponible")

            verify_prompt = (
                "Eres un verificador de calidad objetivo. "
                "Evalúa si el siguiente resultado de IA responde ADECUADAMENTE "
                "a la tarea original, sin alucinar contenido que no esté presente.\n\n"
                f"## Tarea original\n{task[:1000]}\n\n"
                f"## Resultado de {gema}\n{result.content[:3000]}\n\n"
                "Responde SOLO con un JSON en este formato exacto:\n"
                '{"confidence": 0-100, "reason": "explicación breve (max 200 chars)"}'
            )

            messages = [
                LLMMessage(role="system", content="Eres un verificador de calidad objetivo. Evalúa resultados de IA con contexto fresco."),
                LLMMessage(role="user", content=verify_prompt),
            ]

            response = await asyncio.wait_for(
                provider.chat_with_retry(messages=messages),
                timeout=timeout,
            )

            text = response.content or ""
            m = re.search(r'\{[^}]+\}', text)
            if m:
                data = json.loads(m.group())
                confidence = float(data.get("confidence", 50))
                reason = str(data.get("reason", ""))
                return (max(0.0, min(100.0, confidence)), reason)

            return (50.0, "verifier: no se pudo extraer JSON de la respuesta")

        except Exception as e:
            logger.debug(f"[team] Verifier error for {gema}: {e}")
            return (50.0, f"verifier error: {e}")

    def _notify_progress(self, execution: TeamExecution, gema: str, status: str):
        """Notifica progreso vía hub si está disponible."""
        if self.hub:
            try:
                asyncio.create_task(self.hub.publish(
                    event_type="team.progress",
                    content=json.dumps({
                        "execution_id": execution.id,
                        "gema": gema,
                        "status": status,
                        "progress": execution.progress,
                    }),
                    scope="teams",
                ))
            except Exception:
                pass

    # ── DANGEROUS_TOOLS (Fix 6) ────────────────────────────────────
    DANGEROUS_TOOLS = {
        "run_command", "execute_command", "write_file", "delete_file",
        "fs_write", "fs_exec", "shell_exec", "remote_exec",
    }

    # ── Fix 3: Selección inteligente de gema ────────────────────────

    async def _select_gemas(self, task: str) -> List[str]:
        """Fix 3: Selecciona la mejor gema usando classify_task.
        Default: 1 gema + verifier (no 3 paralelas).
        """
        if not self.director:
            return ["code"]
        try:
            classification = await self.director.classify_task(task)
            if classification.selected_gems:
                primary = classification.selected_gems[0]
                logger.info(f"[team] classify_task -> {primary} para: {task[:80]}...")
                return [primary]
        except Exception as e:
            logger.warning(f"[team] classify_task falló, default code: {e}")
        return ["code"]

    # ── Fix 4: Descomposición DAG de tareas ─────────────────────────

    async def _decompose_task(self, task: str) -> Optional[List[SubTask]]:
        """Descompone tarea compleja en DAG de sub-tareas vía LLM.
        Retorna None si la tarea es simple (1 sub-tarea).
        """
        if not self.director:
            return None
        from src.core.provider_base import LLMMessage
        try:
            provider = self.director.provider_registry.get("gema-con-fallback")
            if not provider:
                return None
            prompt = (
                "Descompón la siguiente tarea en sub-tareas atómicas ejecutables "
                "de forma independiente. Cada sub-tarea se asigna a UNA gema.\n\n"
                f"Tarea: {task[:2000]}\n\n"
                "Gemas: code, scholar, architect, analyst, security, debugger, "
                "optimizer, tester, devops, creative, sage, design, engineer\n\n"
                "Responde SOLO JSON. Si es indivisible: {\"single\": true}\n"
                "Si es divisible, array de:\n"
                '{"id":"1","description":"...","gema":"code","depends_on":[]}'
            )
            messages = [
                LLMMessage(role="system", content="Planificador. Descompones tareas en DAGs ejecutables."),
                LLMMessage(role="user", content=prompt),
            ]
            response = await asyncio.wait_for(provider.chat_with_retry(messages=messages), timeout=20.0)
            text = response.content or ""
            m = re.search(r'(\[.*\]|\{.*\})', text, re.DOTALL)
            if not m:
                return None
            data = json.loads(m.group())
            if isinstance(data, dict) and data.get("single"):
                return None
            if not isinstance(data, list) or len(data) < 2:
                return None
            result = []
            for i, item in enumerate(data):
                result.append(SubTask(
                    id=str(item.get("id", i)),
                    description=item.get("description", ""),
                    gema=item.get("gema", "code"),
                    depends_on=item.get("depends_on", []),
                ))
            logger.info(f"[team] DAG: {len(result)} sub-tareas")
            return result
        except Exception as e:
            logger.debug(f"[team] Decomposition skipped: {e}")
            return None

    async def _execute_dag(
        self,
        sub_tasks: List[SubTask],
        timeout: float,
        context: str,
    ) -> TeamExecution:
        """Ejecuta DAG de sub-tareas respetando dependencias."""
        exec_id = str(uuid.uuid4())[:12]
        gemas = sorted(set(st.gema for st in sub_tasks))
        execution = TeamExecution(
            id=exec_id,
            task=f"DAG ({len(sub_tasks)} sub-tareas)",
            gemas=gemas,
        )
        self._executions[exec_id] = execution

        completed: Dict[str, AgentResult] = {}
        remaining = {st.id: st for st in sub_tasks}

        while remaining:
            ready = [st for st in remaining.values() if all(d in completed for d in st.depends_on)]
            if not ready:
                for st_id, st in remaining.items():
                    completed[st_id] = AgentResult(
                        gema=st.gema, success=False, content="",
                        error=f"DAG blocked by dependencies: {st.depends_on}",
                    )
                break

            async def run_one(st: SubTask) -> tuple[str, AgentResult]:
                ctx = context
                for dep_id in st.depends_on:
                    dr = completed.get(dep_id)
                    if dr and dr.success:
                        ctx += f"\n[De {dep_id}]\n{dr.content[:800]}"
                result = await self._run_single_gema(st.gema, st.description, execution, timeout, ctx)
                return (st.id, result)

            results = await asyncio.gather(*[run_one(st) for st in ready], return_exceptions=True)
            for r in results:
                if isinstance(r, tuple):
                    st_id, result = r
                    completed[st_id] = result
                    del remaining[st_id]

        # Guardar resultados con prefijo dag_ y limpiar duplicados de gema
        dag_gemas = set()
        for st in sub_tasks:
            r = completed.get(st.id)
            if r:
                execution.results[f"dag_{st.id}"] = r
                dag_gemas.add(st.gema)
        for k in list(execution.results.keys()):
            if k in dag_gemas:
                del execution.results[k]

        successful = sum(1 for r in completed.values() if r.success)
        execution.status = "done" if successful == len(sub_tasks) else "partial"
        execution.finished_at = time.time()
        return execution

    # ── Fix 5: Consistencia cross-gema ──────────────────────────────

    async def _verify_consistency(self, execution: TeamExecution) -> Dict[str, float]:
        """Fix 5: Verifica coherencia entre resultados de múltiples gemas.
        Retorna dict {gema: confidence} ajustado por consistencia.
        """
        results = {k: v for k, v in execution.results.items() if v.success and v.content}
        if len(results) < 2:
            return {k: 100.0 for k in results}

        from src.core.provider_base import LLMMessage
        try:
            provider = self.director.provider_registry.get("gema-con-fallback")
            if not provider:
                return {k: 100.0 for k in results}

            parts = []
            for gema, r in results.items():
                parts.append(f"### {gema}\n{r.content[:1500]}")
            combined = "\n\n".join(parts)

            prompt = (
                "Revisa si los siguientes análisis de diferentes especialistas "
                "son COHERENTES entre sí o si hay CONTRADICCIONES.\n\n"
                f"{combined}\n\n"
                "Responde SOLO JSON:\n"
                '{"coherente": true/false, "conflictos": ["breve descripción de cada conflicto"]}'
            )
            messages = [
                LLMMessage(role="system", content="Revisor de coherencia multi-experto."),
                LLMMessage(role="user", content=prompt),
            ]
            response = await asyncio.wait_for(provider.chat_with_retry(messages=messages), timeout=15.0)
            text = response.content or ""
            m = re.search(r'\{[^}]+\}', text)
            if m:
                data = json.loads(m.group())
                coherente = data.get("coherente", True)
                base = 100.0 if coherente else 60.0
                return {k: base for k in results}
        except Exception as e:
            logger.debug(f"[team] Consistency check skipped: {e}")

        return {k: 100.0 for k in results}

    # ── Fix 6: Permission gating ────────────────────────────────────

    async def _permission_gated_executor(self, name: str, args: dict) -> Any:
        """Wrapper que añade permission gating a herramientas peligrosas."""
        if name in self.DANGEROUS_TOOLS:
            allowed = await self._request_tool_permission(name, args)
            if not allowed:
                return f"[PERMISSION DENIED] Tool '{name}' requiere aprobación humana. Args: {json.dumps(args)[:200]}"
        return await self.director._multi_motor_tool_executor(name, args)

    async def _request_tool_permission(self, name: str, args: dict) -> bool:
        """Solicita permiso humano para tool peligrosa vía hub + timeout."""
        if not self.hub:
            logger.warning(f"[team] Tool peligrosa '{name}' sin hub de permisos, DENEGADA por defecto")
            return False
        try:
            event_id = str(uuid.uuid4())[:8]
            await self.hub.publish(
                event_type="tool.permission_request",
                content=json.dumps({
                    "id": event_id,
                    "tool": name,
                    "args": {k: v for k, v in args.items() if not isinstance(v, str) or len(v) < 200},
                    "requested_at": time.time(),
                }),
                scope="teams",
            )
            deadline = time.time() + 30.0
            while time.time() < deadline:
                await asyncio.sleep(0.5)
            return False
        except Exception as e:
            logger.warning(f"[team] Permission request error: {e}")
            return False

    # ── Síntesis de resultados ──────────────────────────────────────

    async def _synthesize_results(
        self,
        execution: TeamExecution,
        timeout: float = 30.0,
    ) -> str:
        """Sintetiza resultados de múltiples gemas en una respuesta unificada."""
        successful = {k: v for k, v in execution.results.items() if v.success and v.content}
        if not successful:
            return "No se obtuvieron resultados válidos de las gemas."

        parts = []
        for gema, result in successful.items():
            parts.append(f"### {gema.upper()}\n{result.content[:2000]}")

        combined = "\n\n".join(parts)

        if self.director:
            try:
                synthesis_task = (
                    "Sintetiza los siguientes análisis de múltiples expertos en una respuesta "
                    "coherente y accionable. Mantén los puntos clave de cada experto.\n\n"
                    f"{combined}"
                )
                result = await asyncio.wait_for(
                    self.director.execute(synthesis_task, gem="director"),
                    timeout=timeout,
                )
                if hasattr(result, 'data') and isinstance(result.data, dict):
                    return result.data.get('content', combined)
                return combined
            except Exception:
                return combined
        else:
            return combined

    async def execute(
        self,
        task: str,
        gemas: Optional[List[str]] = None,
        timeout: float = 60.0,
        synthesize: bool = True,
        context: str = "",
        progress_callback: Optional[Callable] = None,
    ) -> TeamExecution:
        """
        Ejecuta una tarea con una o más gemas.

        Fix 3: Cuando gemas es None, selecciona 1 gema vía classify_task (no 3 paralelas).
        Fix 4: Intenta descomposición DAG para tareas complejas antes de ejecutar.
        Fix 5: Verifica consistencia cross-gema cuando múltiples gemas producen resultados.
        """
        # Fix 4: Intentar descomposición DAG primero
        dag_tasks = await self._decompose_task(task)
        if dag_tasks is not None:
            return await self._execute_dag(dag_tasks, timeout, context)

        # Fix 3: Seleccionar gemas si no se especificaron
        if not gemas:
            gemas = await self._select_gemas(task)

        exec_id = str(uuid.uuid4())[:12]
        execution = TeamExecution(id=exec_id, task=task, gemas=gemas)
        self._executions[exec_id] = execution

        logger.info(f"[team] Iniciando ejecución {exec_id} con gemas: {gemas}")

        # Ejecutar gemas en paralelo
        tasks = [
            self._run_single_gema(gema, task, execution, timeout, context)
            for gema in gemas
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Fix 5: Verificar consistencia cross-gema
        if len(gemas) > 1:
            consistency = await self._verify_consistency(execution)
            for gema, conf in consistency.items():
                if gema in execution.results and conf < 80.0:
                    r = execution.results[gema]
                    if r.success:
                        r.success = False
                        r.error = r.error or f"cross_consistency:{conf:.0f}"

        # Determinar estado final
        successful = sum(1 for r in execution.results.values() if r.success)
        execution.status = "done" if successful == len(gemas) else "partial" if successful > 0 else "error"

        # Sintetizar si se pidió
        if synthesize and successful > 0:
            try:
                execution.synthesis = await asyncio.wait_for(
                    self._synthesize_results(execution),
                    timeout=30.0,
                )
            except Exception as e:
                logger.warning(f"[team] Síntesis falló: {e}")
                execution.synthesis = None

        execution.finished_at = time.time()
        logger.info(
            f"[team] Ejecución {exec_id}: {execution.status} "
            f"({successful}/{len(gemas)})"
        )
        return execution

    def get_execution(self, exec_id: str) -> Optional[TeamExecution]:
        return self._executions.get(exec_id)

    def list_executions(self, limit: int = 20) -> List[TeamExecution]:
        execs = sorted(
            self._executions.values(),
            key=lambda e: e.started_at,
            reverse=True,
        )
        return execs[:limit]

    def to_dict(self, execution: TeamExecution) -> Dict:
        """Serializa TeamExecution a dict para API."""
        return {
            "id": execution.id,
            "task": execution.task,
            "gemas": execution.gemas,
            "status": execution.status,
            "results": {
                gema: {
                    "success": r.success,
                    "content": r.content[:500] if r.content else "",
                    "model": r.model,
                    "tokens_used": r.tokens_used,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                }
                for gema, r in execution.results.items()
            },
            "synthesis": execution.synthesis[:1000] if execution.synthesis else None,
            "started_at": execution.started_at,
            "finished_at": execution.finished_at,
            "progress": execution.progress,
        }


_default_board = MessageBoard()
_default_manager = AgentTeamManager(board=_default_board)
_default_executor = ParallelTeamExecutor()
