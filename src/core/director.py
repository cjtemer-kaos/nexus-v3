"""
DirectorNexus v2 - Cerebro central de SuperNEXUS

Combina:
- Runtime loop (Rowboat pattern)
- LLM semantic routing (OpenSwarm pattern)
- Agent capabilities registry (NEXUS pattern)
- Multi-engine orchestration (ConnectivityLayer)
- AI Tools Registry (Brain + Tools pattern)

NEXUS es el cerebro. Los modelos de IA son herramientas stateless.
"""
import asyncio

import json
import os

import aiohttp

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.core.connectivity import EngineResult
from src.core.hooks_engine import HookPhase

# --- New Provider + Runner (Orquestador Multi-Motor) ---
from src.core.provider_base import LLMMessage

# Memory extraction context window
CONTEXT_WINDOW = 6
from src.core.agent_runner import AgentRunner, AgentRunSpec




from src.core.adaptive_router import AdaptiveRouter
from src.core.self_learning_loop import SelfLearningLoop
from src.core.tool_registry import DirectorToolRegistry
# --- F1: Director Soberano ---
from src.core.command_protocol import Command, CommandResult, CommandStatus

# --- F2: Memory Hardening ---
from src.core.memory_triage import MemoryTriage
from src.core.pointer_store import PointerStore
from src.core.dream_consolidation import DreamConsolidator, DreamConfig
from src.core.perplexity_scorer import PerplexityScorer

# --- F3: Protocol Stack ---
from src.core.acp_protocol import ACPRouter
from src.core.a2a_server import A2AServer
from src.core.protocol_router import ProtocolRouter, ServiceEntry, Protocol

# --- F4: Skills Marketplace ---
from src.core.skill_marketplace import SkillRegistry

# --- F6: Code Absorption ---
from src.core.code_absorber import CodeAbsorber

# --- F7: Production Hardening ---
from src.core.circuit_breaker import CircuitBreaker, HealthChecker
from src.core.token_monitor import TokenMonitor

# --- Market Parity: 16 new core modules ---
try:
    from src.core.typed_events import typed_event_bus as _typed_events
except ImportError:
    _typed_events = None
try:
    from src.brain.episodic_memory import EpisodicMemory
except ImportError:
    EpisodicMemory = None
try:
    from src.core.capability_security import get_capability_manager
except ImportError:
    get_capability_manager = None
try:
    from src.core.prompt_compressor import get_compressor
except ImportError:
    get_compressor = None
try:
    from src.core.self_improving import get_self_improving_loop
except ImportError:
    get_self_improving_loop = None
try:
    from src.core.smart_codebase_indexer import SmartCodebaseIndexer
except ImportError:
    SmartCodebaseIndexer = None
try:
    from src.core.multi_file_editor import MultiFileEditor
except ImportError:
    MultiFileEditor = None
try:
    from src.core.task_executor import TaskExecutor
except ImportError:
    TaskExecutor = None
try:
    from src.core.context_manager import ContextManager
except ImportError:
    ContextManager = None
try:
    from src.core.planning_engine import PlanningEngine
except ImportError:
    PlanningEngine = None
try:
    from src.core.quality_judge import QualityJudge
except ImportError:
    QualityJudge = None
try:
    from src.core.auto_skill_creator import AutoSkillCreator
except ImportError:
    AutoSkillCreator = None
try:
    from src.core.skill_lifecycle import get_skill_lifecycle
except ImportError:
    get_skill_lifecycle = None
try:
    from src.core.curator import get_curator
except ImportError:
    get_curator = None
try:
    from src.core.learning_graph import get_learning_graph
except ImportError:
    get_learning_graph = None
try:
    from src.core.voice_engine import get_engine as get_voice_engine
except ImportError:
    get_voice_engine = None

# --- Auth Vault (credential storage) ---
try:
    from src.core.auth_vault import AuthVault
except ImportError:
    AuthVault = None

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

@dataclass
class GemCapability:
    """Capacidad de un Gema"""
    name: str
    tags: List[str]
    description: str
    model: str = ""
    parallel_capable: bool = True
    execution_count: int = 0
    success_count: int = 0
    total_latency_ms: float = 0


# TaskClassification moved to src/brain/routing.py — re-exported here for backward compat
from src.brain.routing import TaskClassification  # noqa: F401, E402


class DirectorNexus:
    """
    DirectorNexus v2 - Orquestador central (CEREBRO)

    SIEMPRE EN MEMORIA (nunca se olvida):
    - Identidad (quien es, su funcion)
    - 15 gemas disponibles
    - Skills registry base
    - Conexiones (SSH, Tailscale, MCP)
    - AI Tools Registry (herramientas de IA)

    MEMORIA SELECTIVA POR PROYECTO:
    - Contexto especifico del proyecto activo
    - Skills cargados para el proyecto
    - Historial de conversaciones del proyecto

    ARQUITECTURA BRAIN + TOOLS:
    - DirectorNexus mantiene contexto, identidad y estado
    - AI Tools son stateless, se invocan con prompts especificos
    - Cada herramienta tiene un rol definido y system prompt acotado
    """

    # Identidad permanente
    IDENTITY = {
        "name": "NEXUS IA",
        "version": "2.0",
        "role": "Cerebro central del ecosistema NEXUS",
        "function": "Coordinar motores, gemas, memoria y herramientas de IA para resolver tareas",
        "architecture": "Brain + Tools (NEXUS es el cerebro, los modelos son herramientas)",
    }

    def __init__(self, project: str = "default", app=None):
        self.identity = self.IDENTITY.copy()
        self.current_project = project
        self._app = app  # NexusApp container (S4 refactor) — optional, backward-compat
        self.execution_log: List[Dict] = []
        self._stats_lock = asyncio.Lock()
        self._project_root = str(Path(__file__).resolve().parent.parent.parent)

        # Project context — loaded from data/projects/<project>/CONTEXT.md
        self._project_context: str | None = None

        # Sticky route cache (AnythingLLM pattern) — avoid reclassifying follow-ups
        self._sticky_cache: Dict[str, dict] = {}  # session_id -> {gems, timestamp}
        self._sticky_ttl_s = 300  # 5 minutes

        # Trace logging — record classify→gema→response for eval and retraining
        self._traces_dir = Path.home() / ".nexus" / "traces"
        self._traces_dir.mkdir(parents=True, exist_ok=True)

        # Distillation buffer — save good responses as future SFT data
        self._distill_dir = Path.home() / ".nexus" / "distillation"
        self._distill_dir.mkdir(parents=True, exist_ok=True)

        # Brain modules (v3 architecture) — cerebro de NEXUS, el director los usa.
        from src.brain.identity import IdentityBrain
        from src.brain.health import HealthBrain
        from src.brain.routing import RoutingBrain
        from src.brain.tools import ToolBrain
        from src.brain.memory import MemoryBrain
        self.identity_brain = IdentityBrain(self)
        self.health_brain = HealthBrain(self)
        self.routing_brain = RoutingBrain(self, sticky_ttl_s=self._sticky_ttl_s)
        self.tool_brain = ToolBrain(self)
        self.memory_brain = MemoryBrain(self)

        # Plugin discovery — scan and load external plugins at startup
        from src.core.plugin_discovery import PluginManager
        self.plugin_manager = PluginManager(
            data_dir=Path(self._project_root) / "data" / "plugins"
        )
        try:
            discovered = self.plugin_manager.discover(
                extra_dirs=[Path(self._project_root) / "plugins"]
            )
            if discovered:
                logger.info(f"Plugins discovered: {len(discovered)} — {', '.join(m.name for m in discovered[:5])}")
        except Exception as e:
            logger.warning(f"Plugin discovery failed: {e}")

        # Webhook manager — fire outgoing webhooks on chat/task completion
        from src.core.webhook_manager import WebhookManager
        self.webhook_manager = WebhookManager()

        # S4 refactor: init via direct calls instead of SystemManager wrapper
        from src.services.core_service import CoreService; CoreService.init_core(self)
        from src.services.orchestration_service import OrchestrationService; OrchestrationService.init_orchestration(self)
        from src.services.memory_service import MemoryService; MemoryService.init_memory(self)
        from src.services.agent_service import AgentService
        n = AgentService.load_gemas(self.gemas)
        logger.info(f"Gemas loaded: {n} (models auto-assigned)")
        from src.services.provider_service import ProviderService; ProviderService.init_providers(self)
        from src.services.tool_service import ToolService; ToolService.init_tooling(self)
        AgentService.init_agents(self)
        from src.services.integration_service import IntegrationService; IntegrationService.init_integrations(self)
        from src.services.actor_service import ActorService
        ActorService.init_actor_system(self)


        self.acp_router = ACPRouter()
        self.a2a_server = A2AServer(executor=self._a2a_execute)
        self.protocol_router = ProtocolRouter()
        self.protocol_router.register_protocol(Protocol.ACP, self.acp_router)
        self.protocol_router.register_protocol(Protocol.A2A, self.a2a_server)
        if hasattr(self, 'external_agents'):
            for agent in self.external_agents.agents:
                proto = Protocol.ACP if agent.protocol == "messageboard" else \
                        Protocol.HTTP if agent.protocol == "http" else \
                        Protocol.MCP if agent.protocol == "mcp" else Protocol.CLI
                self.protocol_router.discovery.register(ServiceEntry(
                    name=agent.name, protocol=proto, endpoint=agent.endpoint, capabilities=agent.capabilities))
        self.skill_registry = SkillRegistry()
        self.health_checker = HealthChecker()
        self.token_monitor = TokenMonitor()
        for agent_name in ["director", "code", "scholar", "analyst", "opencode"]:
            self.health_checker.add_breaker(CircuitBreaker(name=agent_name, failure_threshold=5, recovery_timeout_s=30.0))
        for agent_name in self.gemas:
            name = agent_name if isinstance(agent_name, str) else agent_name.get("name", "unknown")
            self.token_monitor.set_budget(name, 500_000)
        from src.services.training_service import TrainingService; TrainingService.init_training(self)
        self.memory_triage = MemoryTriage()
        self.pointer_store = PointerStore()
        self.dream_consolidator = DreamConsolidator(config=DreamConfig(
            snapshot_dir=str(Path(self._project_root) / ".nexus" / "snapshots")))
        self.perplexity_scorer = PerplexityScorer()
        if hasattr(self, 'hierarchical_memory'):
            for item in self.hierarchical_memory._items:
                self.memory_triage.register_known(item.content)
        self._init_learning_systems()
        from src.services.director_service import DirectorService
        DirectorService.init_director(self)
        self.code_absorber = CodeAbsorber(brain_store=self._absorb_to_brain)

        self.tool_registry = DirectorToolRegistry()
        self.tool_registry.rebuild(self)

        # ── Market Parity modules (lazy init) ──────────────────────────
        self._market_parity_modules = {}
        self._voice_engine = None
        self._init_market_parity()

        logger.info(f"DirectorNexus v2 initialized (project: {project}, architecture: Brain + Tools)")

    def _init_learning_systems(self):
        self._adaptive_router = AdaptiveRouter()
        self._adaptive_sampler = self._adaptive_router._sampler
        self._self_learning = SelfLearningLoop(
            judge_fn=getattr(self, 'judge', None),
            memory_store_fn=lambda k, v: self.hive.remember(k, v) if hasattr(self, 'hive') else None,
            adaptive_router=self._adaptive_router if hasattr(self, '_adaptive_router') else None,
            interval_s=180.0,
        )
        logger.info("AdaptiveRouter + SelfLearningLoop initialized")

    def _init_market_parity(self):
        """Initialize market parity modules (lazy, non-blocking)."""
        modules = {}
        try:
            if EpisodicMemory:
                modules["episodic_memory"] = EpisodicMemory()
        except Exception as e:
            logger.warning(f"EpisodicMemory init failed: {e}")

        try:
            if get_capability_manager:
                modules["capability_manager"] = get_capability_manager()
        except Exception as e:
            logger.warning(f"CapabilityManager init failed: {e}")

        try:
            if get_compressor:
                modules["prompt_compressor"] = get_compressor()
        except Exception as e:
            logger.warning(f"PromptCompressor init failed: {e}")

        try:
            if get_self_improving_loop:
                modules["self_improving"] = get_self_improving_loop()
        except Exception as e:
            logger.warning(f"SelfImprovingLoop init failed: {e}")

        try:
            if SmartCodebaseIndexer:
                modules["codebase_indexer"] = SmartCodebaseIndexer()
        except Exception as e:
            logger.warning(f"SmartCodebaseIndexer init failed: {e}")

        try:
            if MultiFileEditor:
                modules["multi_file_editor"] = MultiFileEditor()
        except Exception as e:
            logger.warning(f"MultiFileEditor init failed: {e}")

        try:
            if TaskExecutor:
                modules["task_executor"] = TaskExecutor()
        except Exception as e:
            logger.warning(f"TaskExecutor init failed: {e}")

        try:
            if ContextManager:
                modules["context_manager"] = ContextManager()
        except Exception as e:
            logger.warning(f"ContextManager init failed: {e}")

        try:
            if PlanningEngine:
                modules["planning_engine"] = PlanningEngine()
        except Exception as e:
            logger.warning(f"PlanningEngine init failed: {e}")

        try:
            if QualityJudge:
                modules["quality_judge"] = QualityJudge()
        except Exception as e:
            logger.warning(f"QualityJudge init failed: {e}")

        try:
            if AutoSkillCreator:
                modules["auto_skill_creator"] = AutoSkillCreator()
        except Exception as e:
            logger.warning(f"AutoSkillCreator init failed: {e}")

        try:
            if get_skill_lifecycle:
                modules["skill_lifecycle"] = get_skill_lifecycle()
        except Exception as e:
            logger.warning(f"SkillLifecycle init failed: {e}")

        try:
            if get_curator:
                modules["curator"] = get_curator()
        except Exception as e:
            logger.warning(f"Curator init failed: {e}")

        try:
            if get_learning_graph:
                modules["learning_graph"] = get_learning_graph()
        except Exception as e:
            logger.warning(f"LearningGraph init failed: {e}")

        try:
            if get_voice_engine:
                self._voice_engine = get_voice_engine()
                if self._voice_engine and self._voice_engine.available:
                    modules["voice_engine"] = self._voice_engine
        except Exception as e:
            logger.warning(f"VoiceEngine init failed: {e}")

        # Typed event bus is a module-level singleton
        if _typed_events:
            modules["typed_events"] = _typed_events

        self._market_parity_modules = modules
        n = len(modules)
        names = ", ".join(sorted(modules.keys()))
        logger.info(f"Market parity modules initialized: {n} ({names})")

    def get_market_parity(self, name: str):
        """Get a market parity module by name."""
        return self._market_parity_modules.get(name)

    # ── Ruta determinista (sin LLM) ─────────────────────────────

    async def _execute_deterministic(self, task: str, context: str = "") -> str:
        return await self.tool_brain.dispatch(task, context)

    async def _web_search(self, query: str) -> str:
        return await self.tool_brain.web_search(query)

    async def _web_navigate(self, url: str) -> str:
        return await self.tool_brain.web_navigate(url)

    async def _browser_snapshot(self, url: str = "", interactive_only: bool = True) -> str:
        return await self.tool_brain.browser_snapshot(url, interactive_only)

    async def _browser_interact(self, ref_or_command: str, value: str = "") -> str:
        return await self.tool_brain.browser_interact(ref_or_command, value)

    async def _browser(self, command: str) -> str:
        return await self.tool_brain.browser(command)

    async def _deep_research(self, query: str, max_time: int = 300) -> str:
        return await self.tool_brain.deep_research(query, max_time=max_time)

    async def _generate_image(self, prompt: str) -> dict | None:
        """Generate image via Vision API (SDXL/Foocus) and save to gallery.
        
        Calls POST http://localhost:9000/api/vision/generate with the prompt,
        then uploads the result to the gallery via POST /api/gallery/upload.
        
        Returns dict with image info (path, seed, gallery) or None on failure.
        """
        base_url = "http://localhost:9000"
        
        # Step 1: Generate image via Vision API
        logger.info(f"Calling Vision API: prompt='{prompt[:80]}'")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url}/api/vision/generate",
                    json={"prompt": prompt},
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"Vision API error {resp.status}: {error_text}")
                        return None
                    gen_result = await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"Vision API connection failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Vision API call failed: {e}")
            return None

        if not gen_result or gen_result.get("error"):
            logger.error(f"Vision API returned error: {gen_result}")
            return None

        # Step 2: If the API already auto-uploaded to gallery, return directly
        if gen_result.get("gallery"):
            logger.info(f"Image generated and saved to gallery: {gen_result.get('path', 'unknown')}")
            return gen_result

        # Step 3: Manual gallery upload if auto-upload didn't happen
        image_path = gen_result.get("path", "")
        if image_path:
            from pathlib import Path as _Path
            p = _Path(image_path)
            if p.exists():
                try:
                    import aiohttp as _aiohttp
                    data = _aiohttp.FormData()
                    data.add_field("file", p.read_bytes(),
                                   filename=p.name,
                                   content_type="image/png")
                    data.add_field("prompt", prompt)
                    data.add_field("tags", "generated,director")
                    data.add_field("model", "sdxl-juggernaut")
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            f"{base_url}/api/gallery/upload",
                            data=data,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as resp:
                            if resp.status == 200:
                                gallery_result = await resp.json()
                                gen_result["gallery"] = gallery_result
                                logger.info(f"Image saved to gallery: {gallery_result}")
                            else:
                                logger.warning(f"Gallery upload failed: {resp.status}")
                except Exception as e:
                    logger.warning(f"Gallery upload failed: {e}")

        return gen_result

    async def _mcp_call(self, tool_name: str, arguments: dict) -> str:
        return await self.tool_brain.mcp_call(tool_name, arguments)

    async def _list_mcp_tools(self, server: str = "") -> str:
        return await self.tool_brain.list_mcp_tools(server)

    def _get_memory_context(self, task: str, limit: int = 5) -> str:
        # Delegado a MemoryBrain (src/brain/memory.py)
        return self.memory_brain.get_memory_context(task, limit=limit)

    # ── Health checks — delegado a HealthBrain (src/brain/health.py) ────

    def _health_core(self) -> bool:
        return self.health_brain.check_core()

    def _health_memory(self) -> bool:
        return self.health_brain.check_memory()

    def _health_gemas(self) -> bool:
        return self.health_brain.check_gemas()

    def _health_providers(self) -> bool:
        return self.health_brain.check_providers()

    async def initialize_async(self):
        """Initialize async components (RAG, self-model). Call after __init__."""
        # RAG Engine
        try:
            await self.rag_engine.initialize()
            logger.info("RAG engine initialized")
        except Exception as e:
            logger.error(f"RAG engine init failed: {e}")

        # Self-model
        try:
            await self.self_model.initialize()
            logger.info("Self-model initialized with auto-discovery")
        except Exception as e:
            logger.error(f"Self-model initialization failed: {e}")

    def _recover_session_context(self, project: str):
        # Delegado a MemoryBrain (src/brain/memory.py)
        self.memory_brain.recover_session_context(project)

    def persist_session_state(self, session_id: str, project: str, messages: List[Dict], tokens: int = 0):
        # Delegado a MemoryBrain (src/brain/memory.py)
        self.memory_brain.persist_session_state(session_id, project, messages, tokens)

    async def _agent_loop_llm(self, prompt: str, model: str = "qwen2.5-coder:7b") -> str:
        """Bridge for AgentLoop → Ollama via ConnectivityLayer."""
        try:
            result = await self.ai_tools.quick_response(
                task=prompt, gem="director", context="", model_override=model
            )
            return result.get("content", str(result))
        except Exception as e:
            return f"Error: {e}"

    async def run_agent_loop(self, task: str, context: str = "") -> dict:
        """Run TDAO agent loop for complex multi-step tasks."""
        result = await self.agent_loop.run(task, context)
        return {
            "success": result.success,
            "output": result.final_output,
            "iterations": result.iterations,
            "steps": len(result.steps),
            "duration_ms": result.total_duration_ms,
        }

    async def classify_task(self, task: str, session_id: str = "") -> TaskClassification:
        """Clasifica tarea — delegado a RoutingBrain (src/brain/routing.py)
        o RoutingService (S4 refactor).

        Mantiene compatibilidad: convierte el TaskClassification del brain
        al de director.py (son la misma dataclass de hecho — ver imports).
        """
        if self._app is not None and self._app.has("routing"):
            return self._app.get("routing").classify(task, session_id=session_id)
        return await self.routing_brain.classify(task, session_id=session_id)

    async def _llm_classify(self, task: str) -> Optional[str]:
        """LLM classify — delegado a RoutingBrain."""
        return await self.routing_brain._llm_classify(task)

    async def get_relevant_skills(self, task: str, top_k: int = 3) -> str:
        """Get relevant skill content for task context."""
        matched = self.skill_loader.match_skills(task, top_k=top_k)
        if not matched:
            return ""
        contents = []
        for name in matched:
            content = self.skill_loader.load_skill(name)
            if content and not content.startswith("Skill not found") and not content.startswith("Error"):
                contents.append(f"## Skill: {name}\n{content[:2000]}")
        return "\n\n".join(contents)
    # ── Actor System (Sprint 2) ──────────────────────────────────


    async def orchestrate_multi_motor(self, task: str, context: str = "",
                                       providers: list[str] | None = None) -> dict:
        """
        Orquestación multi-motor: ejecuta la misma tarea con múltiples
        proveedores/gemas en paralelo, evalúa con JudgePipeline, sintetiza.

        Args:
            task: Tarea a ejecutar
            context: Contexto adicional
            providers: Lista de proveedores a usar (default: todos)
        Returns:
            Dict con resultados individuales, evaluación y síntesis
        """
        from datetime import datetime

        if not providers:
            providers = ["ollama-gema", "ollama-local"]
            if self.token_budget.is_within_budget():
                providers.append("ollama-fallback")

        start = datetime.now()
        provider_instances = []
        for name in providers:
            p = self.provider_registry.get(name)
            if p:
                provider_instances.append((name, p))

        if not provider_instances:
            return {"success": False, "error": "No providers available"}

        tool_schemas = self.tool_caller.get_tool_schemas() if hasattr(self, 'tool_caller') else []
        spec = AgentRunSpec(
            messages=[LLMMessage(role="user", content=task)],
            tools_definitions=tool_schemas,
            max_iterations=3,
        )

        async def run_single(name: str, provider) -> dict:
            try:
                runner = AgentRunner(provider, tool_executor=self._multi_motor_tool_executor)
                result = await runner.run(spec)
                return {"name": name, "success": result.stop_reason != "error",
                        "content": result.content[-800:] if result.content else "",
                        "stop_reason": result.stop_reason,
                        "usage": result.usage, "error": None}
            except Exception as e:
                logger.exception("Provider %s failed", name)
                return {"name": name, "success": False, "content": "", "error": str(e)}

        tasks = [run_single(name, p) for name, p in provider_instances]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        best = None
        for r in results:
            if r.get("success") and r.get("content"):
                if best is None or len(r["content"]) > len(best["content"]):
                    best = r

        evaluations = []
        if best and best.get("content"):
            verdict = self.judge.evaluate(task, best["content"])
            evaluations.append({"provider": best["name"], "verdict": str(verdict)})

        duration = (datetime.now() - start).total_seconds()

        return {
            "success": best is not None,
            "providers_tried": providers,
            "results": results,
            "best": best,
            "evaluations": evaluations,
            "duration_s": duration,
        }

    async def orchestrate(self, goal: str) -> dict:
        """Orquestación completa: decompose → execute → synthesize.
        Usa el NexusOrchestrator con descomposición LLM.
        """
        from datetime import datetime
        start = datetime.now()

        if not hasattr(self, 'orchestrator'):
            return {"success": False, "error": "Orchestrator not initialized"}

        try:
            result = await self.orchestrator.orchestrate(goal)

            # Judge evaluation
            if result.synthesis:
                verdict = self.judge.evaluate(goal, result.synthesis)
                judge_result = {"action": verdict.action.value, "feedback": verdict.feedback}
            else:
                judge_result = {"action": "skip", "feedback": "No content to evaluate"}

            return {
                "success": result.success,
                "goal": goal,
                "dag_id": result.dag.id if result.dag else "",
                "tasks": len(result.task_results),
                "completed": sum(1 for r in result.task_results.values() if r["status"] == "completed"),
                "failed": sum(1 for r in result.task_results.values() if r["status"] == "failed"),
                "task_results": result.task_results,
                "synthesis": result.synthesis,
                "judge": judge_result,
                "duration_s": result.duration_s,
            }
        except Exception as e:
            logger.exception("Orchestration failed")
            return {"success": False, "error": str(e), "duration_s": (datetime.now() - start).total_seconds()}

    async def multi_motor_status(self) -> dict:
        """Estado completo del sistema multi-motor."""
        providers = {}
        if hasattr(self, 'provider_registry'):
            providers = await self.provider_registry.health_check()
        orch_status = {}
        if hasattr(self, 'orchestrator'):
            orch_status = self.orchestrator.status()
        return {
            "available": hasattr(self, 'provider_registry'),
            "providers": providers,
            "provider_count": len(providers),
            "orchestrator": orch_status,
        }

    async def execute(self, task: str, gem: str = "auto", context: str = "", images: list = None, session_id: str = None, selected_model: str = "") -> EngineResult:
        """Ejecuta tarea: clasifica -> dispatch -> aprende. S4 refactor: delegacion a ExecutionService."""
        from src.services.execution_service import ExecutionService as ES
        start = datetime.now()
        self._request_selected_model = selected_model or ""

        context = (f"{ES.identity_blurb(self)}\n\n{context}") if context else ES.identity_blurb(self)

        if hasattr(self, 'learning_loop') and self.learning_loop.has_gap(task):
            try: await self.learning_loop.learn(task)
            except Exception as e: logger.warning(f"LearningLoop error: {e}")

        goal_analysis = self.goal_detector.analyze(task)
        if goal_analysis.bypass_coordinator and gem == "auto":
            gem = goal_analysis.suggested_gem

        session = self.sessions.get_session()

        if not self.token_budget.is_within_budget():
            return EngineResult(success=False, data={"error": "Token budget exceeded"}, engine="token_budget", duration_ms=0)

        hook_result = await self.hooks.run_hooks(HookPhase.PRE_EXECUTE, {"task": task, "token_budget": self.token_budget})
        if not hook_result.allow:
            return EngineResult(success=False, data={"error": hook_result.message}, engine="hooks", duration_ms=0)
        if hook_result.modified_input:
            task = hook_result.modified_input.get("task", task)
            context = hook_result.modified_input.get("context", context)

        try:
            mem_ctx = self._get_memory_context(task, limit=3)
            if mem_ctx and mem_ctx.strip():
                task = f"""{task}

[Contexto]: {mem_ctx[:500]}"""
        except Exception:
            pass

        session_id = session.session_id if hasattr(session, 'session_id') else ""
        classification = await self.classify_task(task, session_id=session_id)
        if gem != "auto":
            classification.selected_gems = [gem]
        primary_gem = classification.selected_gems[0] if classification.selected_gems else "director"

        # MCP tool call shortcut
        if task.startswith("mcp__"):
            parts = task.split("__", 2)
            if len(parts) == 3:
                _, server, tool = parts
                mcp_args = {}
                if context:
                    try: mcp_args = json.loads(context)
                    except json.JSONDecodeError: mcp_args = {"query": context}
                mcp_result = await self.mcp_client.call_tool(f"mcp__{server}__{tool}", mcp_args)
                return EngineResult(success="error" not in mcp_result, data=mcp_result, engine="mcp_client",
                                    duration_ms=(datetime.now() - start).total_seconds() * 1000)

        # Deep Research shortcut
        if task.startswith("research__"):
            query = task[len("research__"):].strip()
            result = await self._deep_research(query)
            return EngineResult(success=True, data={"content": result}, engine="deep_research",
                                duration_ms=(datetime.now() - start).total_seconds() * 1000)

        # Dispatch pipeline (S4: extracted to ExecutionService)
        # Para preguntas: Scholar investiga → Sage guarda → Director responde
        ai_result = None
        _action_kw = {"escribe","crea","haz","genera","programa","codigo","funcion","implementa",
                      "refactoriza","arregla","debug","test","prueba","instala","configura",
                      "convierte","compara","analiza","disena","construye","despliega",
                      "generar imagen","genera imagen","dibuja","dibujar","ilustrar",
                      "ilustracion","imagen","picture","artwork","concept art","logo",
                      "pinta","escribe texto","agrega texto","dibuja rectangulo","dibuja circulo",
                      "dibuja linea","agrega forma","agrega imagen","edita imagen"}
        _is_action = any(kw in task.lower().split() for kw in _action_kw)

        # Heuristica: preguntas conversacionales/self-referenciales NO necesitan web research
        _conversational_kw = {
            "tu", "tu/", "tus", "como funciona tu", "que puedes", "que sabes",
            "capacidades", "ayuda", "help", "hola", "hello", "que onda",
            "quien eres", "que eres", "cuantos", "cuales son tus",
            "ensename", "explicame", "hablame", "cuentame",
            "como estas", "que tal", "buenas",
            "funcionas", "funciona", "sirve", "sirves", "trabajas", "trabaja",
            "estas bien", "eres real", "soy tu", "que opinion",
            "gracias", "por favor", "dale", "sigue", "para",
        }
        _is_conversational = any(kw in task.lower() for kw in _conversational_kw)

        if not _is_action and not _is_conversational:
            # Es pregunta factual — PRIMERO buscar en el cerebro/biblioteca
            brain_answer = None
            try:
                from src.brain.cerebro import Cerebro
                cerebro = Cerebro()
                brain_results = cerebro.obtener_conocimientos()
                # Buscar conocimientos relevantes a la pregunta
                task_words = set(re.findall(r'\w+', task.lower()))
                relevant = []
                for k in brain_results:
                    tema = (k.get('tema', '') or '').lower()
                    contenido = (k.get('contenido', '') or '').lower()
                    if any(w in tema or w in contenido for w in task_words if len(w) > 3):
                        relevant.append(k)
                if relevant:
                    brain_context = "\n".join([
                        f"- [{r.get('tema','')}]: {r.get('contenido','')[:500]}"
                        for r in relevant[:3]
                    ])
                    logger.info(f"Brain hit for: {task[:50]} ({len(relevant)} results)")
                    brain_answer = brain_context
            except Exception as e:
                logger.debug(f"Brain recall failed: {e}")

            if brain_answer:
                # El cerebro tiene la respuesta — usar LLM para sintetizar
                try:
                    from src.agents.sage_gem import SageGem
                    sage = SageGem()
                    sage.save_to_library(
                        title=task[:100],
                        content=f"## {task}\n\nConocimiento del cerebro:\n{brain_answer}",
                        topic=sage._infer_topic(brain_answer, task),
                        source="brain_recall"
                    )
                except Exception:
                    pass

                try:
                    _user_model = self._request_selected_model or self.ai_tools.get_default_model()
                    synthesis_prompt = f"""Responde la siguiente pregunta usando el conocimiento del cerebro de NEXUS.
Sé directo y conciso. No inventes información adicional.

CONOCIMIENTO DEL CEREBRO:
{brain_answer}

PREGUNTA: {task}

RESPUESTA:"""
                    synthesized = await self.ai_tools.quick_response(
                        synthesis_prompt, model=_user_model
                    )
                    return {
                        "reply": synthesized.get("reply", ""),
                        "gem_used": "director",
                        "engines": ["nexus_master"],
                    }
                except Exception as e:
                    logger.debug(f"Brain synthesis failed: {e}")

            # Si el cerebro no tiene la respuesta, usar LLM directamente
            # (web research es lenta y a menudo innecesaria)
            logger.info(f"Using LLM direct for: {task[:50]}")
            try:
                synthesis_prompt = f"""Responde la siguiente pregunta de forma directa y concisa.
Si no tienes datos actualizados, indícalo. No inventes información.
Sé práctico y da ejemplos cuando sea relevante.

PREGUNTA: {task}

RESPUESTA:"""
                _user_model = self._request_selected_model or self.ai_tools.get_default_model()
                synthesized = await self.ai_tools.quick_response(
                    task=synthesis_prompt, gem="director", context=context,
                    model_override=_user_model
                )
                if synthesized and isinstance(synthesized, dict) and synthesized.get("content"):
                    ai_result = {
                        "success": True,
                        "content": synthesized["content"],
                        "tool": "scholar_llm",
                        "model": synthesized.get("model", _user_model),
                        "tokens_used": synthesized.get("tokens_used", 0),
                        "duration_ms": 0,
                    }
            except Exception as e:
                    logger.debug(f"LLM fallback failed: {e}")
        elif _is_conversational:
            # Pregunta conversacional - responder directamente con LLM sin web research
            logger.info(f"Conversational question, skipping web research: {task[:50]}")
            try:
                synthesis_prompt = f"""Responde la siguiente pregunta de forma natural y directa. 
Sé conciso. No inventes información. Si no sabes algo, di que no sabes.

PREGUNTA: {task}

RESPUESTA:"""
                _user_model = self._request_selected_model or self.ai_tools.get_default_model()
                logger.info(f"Conversational LLM using model: {_user_model}")
                synthesized = await self.ai_tools.quick_response(
                    task=synthesis_prompt, gem="director", context=context,
                    model_override=_user_model
                )
                if synthesized and isinstance(synthesized, dict) and synthesized.get("content"):
                    ai_result = {
                        "success": True,
                        "content": synthesized["content"],
                        "tool": "llm_direct",
                        "model": synthesized.get("model", _user_model),
                        "tokens_used": synthesized.get("tokens_used", 0),
                        "duration_ms": 0,
                    }
            except Exception as e:
                logger.debug(f"LLM direct failed: {e}")

        # Vision generation branch — if routing classified this as 'vision', generate image
        if ai_result is None and "vision" in (classification.selected_gems or []):
            # Skip generation for conceptual questions about vision
            _vision_conceptual = any(kw in task.lower() for kw in [
                "que es", "explica", "como funciona", "para que sirve",
                "diferencia entre", "vs", "ventajas", "desventajas",
                "define", "concepto", "definicion", "significa",
            ])
            if _vision_conceptual:
                # Let it fall through to LLM direct below
                pass
            else:
                logger.info(f"Vision generation request: {task[:80]}")
            # Extract the image prompt from the task (remove common prefixes)
            prompt = task
            for prefix in ["generar imagen de ", "genera imagen de ", "genera una imagen de ",
                           "crea una imagen de ", "dibuja ", "dibujar ", "ilustrar ",
                           "ilustracion de ", "imagen de ", "picture of ", "artwork de ",
                           "concept art de ", "logo de ", "retrato de ", "paisaje de "]:
                if task.lower().startswith(prefix):
                    prompt = task[len(prefix):]
                    break
            if not prompt or prompt == task:
                # Fallback: use the whole task as prompt
                prompt = task
            try:
                image_info = await self._generate_image(prompt)
                if image_info:
                    content_parts = [f"🎨 **Imagen generada**\n"]
                    content_parts.append(f"**Prompt:** {prompt}")
                    if image_info.get("path"):
                        content_parts.append(f"**Archivo:** {image_info['path']}")
                    if image_info.get("gallery", {}).get("id"):
                        content_parts.append(f"**Galería:** Guardada (ID: {image_info['gallery']['id']})")
                    if image_info.get("seed"):
                        content_parts.append(f"**Seed:** {image_info['seed']}")
                    ai_result = {
                        "success": True,
                        "content": "\n".join(content_parts),
                        "tool": "vision_generate",
                        "model": "sdxl-juggernaut",
                        "tokens_used": 0,
                        "duration_ms": 0,
                        "image_info": image_info,
                    }
                else:
                    ai_result = {
                        "success": False,
                        "content": f"❌ No se pudo generar la imagen. Verifica que el servidor Vision esté activo en localhost:9000.",
                        "tool": "vision_generate",
                        "model": "sdxl-juggernaut",
                        "tokens_used": 0,
                        "duration_ms": 0,
                    }
            except Exception as e:
                logger.error(f"Vision generation failed: {e}")
                ai_result = {
                    "success": False,
                    "content": f"❌ Error al generar imagen: {e}",
                    "tool": "vision_generate",
                    "model": "sdxl-juggernaut",
                    "tokens_used": 0,
                    "duration_ms": 0,
                }

        if ai_result is None:
            ai_result = await ES.try_scholar_gem(self, task, primary_gem, classification)
        if ai_result is None:
            ai_result = await ES.try_agent_runner(self, task, context, primary_gem, session)
        if ai_result is None:
            ai_result = await ES.try_gema_fallback(self, task, context, primary_gem)
        if ai_result is None:
            det_result = await self._execute_deterministic(task, context)
            if det_result:
                ai_result = {"success": True, "content": det_result, "tool": "deterministic", "model": "none",
                             "tokens_used": 0, "duration_ms": 0}

        # Human Layer — post-process content for naturalness
        if ai_result and ai_result.get("content"):
            try:
                from src.core.human_layer import humanize_output, evaluate_naturalness
                orig_len = len(ai_result["content"])
                ai_result["content"] = humanize_output(ai_result["content"])
                eval_result = evaluate_naturalness(ai_result["content"])
                ai_result["human_layer"] = {
                    "orig_len": orig_len,
                    "naturalness_score": eval_result["score"],
                    "is_natural": eval_result["is_natural"],
                }
                if not eval_result["is_natural"]:
                    logger.info(f"Human Layer: low naturalness {eval_result['score']} for task={task[:60]}")
                    ai_result["human_layer"]["feedback"] = eval_result.get("suggestions", [])
            except ImportError:
                pass  # human_layer not available
            except Exception as e:
                logger.debug(f"Human layer skipped: {e}")

        # Auto-fallback: si la gema no sabe, investiga en internet
        if ai_result and ai_result.get("content") and primary_gem != "scholar":
            content_lower = ai_result["content"].lower()
            _ignorance = [
                "no tengo datos", "no tengo información", "no tengo acceso",
                "no puedo", "no sé", "no conozco", "no dispongo",
                "sin acceso a datos", "no tengo conocimiento",
                "no puedo responder", "no puedo decirte",
                "no es posible", "no tengo certeza",
                "desconozco", "no manejo esa información",
                "no tengo esa información", "no cuento con",
            ]
            if any(phrase in content_lower for phrase in _ignorance):
                logger.info(f"Gema '{primary_gem}' no sabe, fallback a scholar: {task[:60]}")
                try:
                    from src.agents.scholar_gem import ScholarGem
                except Exception as e:
                    logger.debug(f"Scholar fallback failed: {e}")

        tokens_used = ai_result.get("tokens_used", 0)
        budget_check = self.token_budget.record_tokens(tokens_used, source=f"gem:{primary_gem}")

        with self.sessions._lock:
            self.sessions.add_message("user", task, session_id=session.id)
            self.sessions.add_message("assistant", ai_result.get("content", ""), tokens=tokens_used, session_id=session.id)
            current_tokens = session.total_tokens

        # Auto-extracción de memoria post-respuesta
        try:
            from src.core.memory_extractor import get_memory_extractor
            extractor = get_memory_extractor(director=self)
            session_msgs = session.get_messages_for_llm(max_messages=CONTEXT_WINDOW, scrub=False)
            _extraction_task = asyncio.create_task(extractor.after_response(
                messages=[{"role": m["role"], "content": m["content"]} for m in session_msgs if m.get("content")],
                session_id=session.id,
                owner=getattr(session, 'owner', ''),
            ))
            if not hasattr(self, '_background_tasks'):
                self._background_tasks = set()
            self._background_tasks.add(_extraction_task)
            _extraction_task.add_done_callback(self._background_tasks.discard)
        except Exception as e:
            logger.debug(f"Memory extraction skipped: {e}")

        if self.sessions.needs_compact(session.id):
            self.sessions.compact_session_trajectory(session.id)

        result_data = ES.build_result_data(self, ai_result, classification, tokens_used)
        result_data["budget"] = budget_check
        result_data["session"] = {"id": session.id, "tokens": current_tokens}
        success = ai_result.get("success", False)

        if classification.can_parallelize and len(classification.selected_engines) > 1:
            try:
                broadcast_results = await asyncio.wait_for(
                    self.connectivity.broadcast(task, engines=classification.selected_engines[1:]), timeout=30.0)
                result_data["parallel"] = {name: r.data for name, r in broadcast_results.items()}
            except asyncio.TimeoutError: pass

        if success and result_data.get("content"):
            verdict = self.judge.evaluate(task=task, result=result_data.get("content", ""),
                                           tool_results=[{"tool": primary_gem, "status": "success"}])
            result_data["judge"] = {"action": verdict.action.value, "feedback": verdict.feedback,
                                    "confidence": verdict.confidence, "level": verdict.level}

        self.cursor.save_state(agent_id=primary_gem, iteration=len(self.execution_log), task=task[:200],
                                outputs={"content": result_data.get("content", "")[:500]},
                                status="completed" if success else "failed")

        duration = (datetime.now() - start).total_seconds() * 1000
        self.execution_log.append({"timestamp": datetime.now().isoformat(), "task": task[:200],
                                    "gems": classification.selected_gems, "engines": classification.selected_engines,
                                    "success": success, "duration_ms": duration})

        async with self._stats_lock:
            for gem_name in classification.selected_gems:
                if gem_name in self.gemas:
                    self.gemas[gem_name].execution_count += 1
                    if success: self.gemas[gem_name].success_count += 1
                    self.gemas[gem_name].total_latency_ms += duration

        judge_quality = result_data.get("judge", {}).get("confidence", 0.0) if result_data.get("judge") else 0.0
        self.self_model.record_outcome(task=task, gema_used=primary_gem, success=success,
                                        quality=judge_quality if success else 0.0, latency_ms=duration)

        if hasattr(self, 'orchestrator') and success and result_data.get("content"):
            await self.hooks.run_hooks(HookPhase.LEARN, {"task": task, "result": result_data["content"][:500],
                                                           "gem": primary_gem})

        await self.hooks.run_hooks(HookPhase.POST_EXECUTE, {"task": task, "success": success,
                                                               "result": result_data.get("content", "")[:500],
                                                               "files_modified": []})

        if success and result_data.get("content"):
            session = self.sessions.get_session(session_id)
            if len(session.messages) >= 4:
                consolidation = await self.memory_consolidator.run_pipeline(
                    [m.to_dict() for m in session.messages[-10:]])
                if consolidation.get("status") == "success":
                    logger.info(f"Memory consolidated: {consolidation.get('facts_extracted', 0)} facts")

            # Auto-extracción de skills de sesiones complejas
            try:
                from src.core.skill_extractor import get_skill_extractor
                skill_extractor = get_skill_extractor(director=self)
                session_msgs = [{"role": m.role, "content": m.content} for m in session.messages[-CONTEXT_WINDOW:]]
                # Extraer tool calls del execution log reciente
                recent_tool_calls = []
                for log_entry in self.execution_log[-3:]:
                    if log_entry.get("tool"):
                        recent_tool_calls.append({
                            "name": log_entry.get("tool", ""),
                            "args": {},
                            "success": log_entry.get("success", True),
                        })
                asyncio.create_task(skill_extractor.after_session(
                    messages=session_msgs,
                    tool_calls=recent_tool_calls,
                    session_id=session.id,
                ))
            except Exception as e:
                logger.debug(f"Skill extraction skipped: {e}")

        if self.sessions.needs_compact(session.id):
            await self.hooks.run_hooks(HookPhase.ON_COMPACT, {"session_id": session.id, "tokens": session.total_tokens})

        final_result = EngineResult(success=success, data=result_data, engine="ai_tools", duration_ms=duration)

        # Trace + distillation
        response_text = result_data.get("content", "")
        try:
            trace = {"timestamp": datetime.now().isoformat(), "task": task,
                     "gems": classification.selected_gems, "engines": classification.selected_engines,
                     "response": response_text[:500] if isinstance(response_text, str) else str(response_text)[:500],
                     "success": success, "duration_ms": duration, "session_id": session_id}
            trace_file = self._traces_dir / f"traces_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            with open(trace_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(trace, ensure_ascii=False) + "\n")
        except Exception: pass

        try:
            if success and response_text and len(str(response_text)) > 20:
                distill_entry = {"messages": [{"role": "user", "content": task},
                                              {"role": "assistant", "content": str(response_text)[:1000]}],
                                 "category": classification.selected_gems[0] if classification.selected_gems else "general",
                                 "source": "distillation_auto"}
                distill_file = self._distill_dir / "distilled_sft.jsonl"
                with open(distill_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(distill_entry, ensure_ascii=False) + "\n")
        except Exception: pass

        # Fire outgoing webhooks — chat.completed + task.completed
        if hasattr(self, 'webhook_manager'):
            try:
                self.webhook_manager.fire_and_forget("chat.completed", {
                    "task": task[:200],
                    "content": result_data.get("content", "")[:1000],
                    "gems": classification.selected_gems,
                    "engines": classification.selected_engines,
                    "success": success,
                    "duration_ms": duration,
                    "tokens_used": tokens_used,
                    "session_id": session_id,
                    "tool_used": primary_gem,
                })
                self.webhook_manager.fire_and_forget("task.completed", {
                    "task": task[:200],
                    "gems": classification.selected_gems,
                    "success": success,
                    "duration_ms": duration,
                })
            except Exception as e:
                logger.debug(f"Webhook fire skipped: {e}")

        return final_result

    async def _research_and_persist(self, task: str, context: str, session) -> dict | None:
        """Flujo: Web research → LLM synthesis → respuesta con fuentes."""
        try:
            # 1. Buscar en web directamente (sin pasar por scholar gem)
            if not hasattr(self, 'web_researcher') or not self.web_researcher:
                logger.debug("No web_researcher available")
                return None
            
            sources = await self.web_researcher.search(task, max_results=3)
            if not sources:
                logger.debug(f"No web sources for: {task[:50]}")
                return None
            
            # 2. Construir contexto de fuentes
            sources_text = "\n".join([
                f"- {s.get('title', '')}: {s.get('snippet', '')[:200]}"
                for s in sources
            ])
            context_from_sources = "\n".join([
                f"[{s.get('title', '')}]({s.get('url', '')})\n{s.get('snippet', '')[:300]}"
                for s in sources
            ])
            
            # 3. Sintetizar respuesta con LLM
            synthesis_prompt = f"""Basándote en estas fuentes web, responde la pregunta del usuario de forma completa y precisa.
Usa español. Si la información es de fuentes en otro idioma, traduce y adapta.
Incluye los enlaces de las fuentes al final.

PREGUNTA: {task}

FUENTES:
{context_from_sources}

RESPUESTA:"""
            
            try:
                from src.agents.sage_gem import SageGem
                sage = SageGem()
                sage.save_to_library(
                    title=task[:100],
                    content=f"## {task}\n\nFuentes encontradas:\n{sources_text}",
                    topic=sage._infer_topic(sources_text, task),
                    source="web_research"
                )
            except Exception as e:
                logger.debug(f"Sage save failed: {e}")
            
            try:
                _user_model = self._request_selected_model or self.ai_tools.get_default_model()
                synthesized = await self.ai_tools.quick_response(
                    task=synthesis_prompt, gem="director", context="",
                    model_override=_user_model
                )
                reply_content = synthesized.get("content", "") if isinstance(synthesized, dict) else str(synthesized)
                
                if reply_content and len(reply_content) > 50:
                    links = "\n\n**Fuentes:**\n" + "\n".join(
                        f"- [{s.get('title', s.get('url', ''))}]({s.get('url', '')})"
                        for s in sources if s.get("url")
                    )
                    return {
                        "success": True,
                        "content": reply_content + links,
                        "tool": "web_research",
                        "model": "deepseek-v4-flash-free",
                        "tokens_used": synthesized.get("tokens_used", 0) if isinstance(synthesized, dict) else 0,
                        "duration_ms": 0,
                    }
            except Exception as e:
                logger.debug(f"LLM synthesis failed: {e}")
            
            # Fallback: retornar fuentes crudas
            return {
                "success": True,
                "content": sources_text,
                "tool": "web_research_raw",
                "model": "web",
                "tokens_used": 0,
                "duration_ms": 0,
            }
            
        except Exception as e:
            logger.debug(f"Research failed: {e}")
            return None
            
            # 3. Director genera respuesta sintetizada con su propio LLM
            context_from_sources = "\n".join([
                f"[{s.get('title', '')}]({s.get('url', '')})\n{s.get('snippet', '')[:300]}"
                for s in research.get("sources", [])
            ])
            
            synthesis_prompt = f"""Basándote en estas fuentes web, responde la pregunta del usuario de forma completa y concisa.
Usa español. Si la información es de fuentes en otro idioma, traduce y adapta.
Incluye los enlaces de las fuentes al final.

PREGUNTA: {task}

FUENTES:
{context_from_sources}

RESPUESTA:"""
            
            try:
                _user_model = self._request_selected_model or self.ai_tools.get_default_model()
                synthesized = await self.ai_tools.quick_response(
                    task=synthesis_prompt,
                    gem="director",
                    context="",
                    model_override=_user_model
                )
                reply_content = synthesized.get("content", "") if isinstance(synthesized, dict) else str(synthesized)
                
                if reply_content and len(reply_content) > 50:
                    # Agregar enlaces de fuentes al final
                    links = "\n\n**Fuentes:**\n" + "\n".join(
                        f"- [{s.get('title', s.get('url', ''))}]({s.get('url', '')})"
                        for s in research.get("sources", []) if s.get("url")
                    )
                    return {
                        "success": True,
                        "content": reply_content + links,
                        "tool": "scholar_research",
                        "model": "deepseek-v4-flash-free",
                        "tokens_used": synthesized.get("tokens_used", 0) if isinstance(synthesized, dict) else 0,
                        "duration_ms": 0,
                    }
            except Exception as e:
                logger.debug(f"LLM synthesis failed: {e}")
            
            # Fallback: retornar fuentes crudas si la sintesis falla
            return {
                "success": True,
                "content": sources_text,
                "tool": "scholar_research",
                "model": "scholar",
                "tokens_used": 0,
                "duration_ms": 0,
            }
            
        except Exception as e:
            logger.debug(f"Research and persist failed: {e}")
            return None

    async def get_dynamic_identity(self) -> Dict:
        # Delegado a IdentityBrain (src/brain/identity.py)
        return await self.identity_brain.get_dynamic_identity()

    # ── System prompt building: delegated to IdentityBrain (src/brain/identity.py) ──

    def _build_system_prompt(self) -> str:
        return self.identity_brain.build_system_prompt()

    def _get_stable_prompt(self) -> str:
        return self.identity_brain.get_stable_prompt()

    def _get_context_prompt(self) -> str:
        return self.identity_brain.get_context_prompt()

    def _get_volatile_prompt(self) -> str:
        return self.identity_brain.get_volatile_prompt()

    # Alias backward-compatible
    _build_director_system_prompt = _build_system_prompt

    async def get_capabilities_report(self) -> str:
        """
        Genera un reporte en lenguaje natural de todo lo que el Director puede hacer.
        Ideal para responder "que puedes hacer?" o inyectar como system prompt.
        """
        identity = await self.get_dynamic_identity()
        lines = []
        lines.append(f"Soy {identity['name']} v{identity['version']}.")
        lines.append(identity['role'])
        lines.append(f"Arquitectura: {identity['architecture']}")
        lines.append("Nombre interno: DirectorNexus v2.0")
        lines.append("")

        # Gemas
        g = identity.get("gemas", {})
        lines.append(f"Tengo {g.get('total', 0)} gemas especializadas disponibles:")
        for name, info in g.get("list", {}).items():
            rate = info.get("success_rate", 0)
            lines.append(f"  - {name}: {info['description']} ({info['model']}) - {info['executions']} ejecuciones, {rate:.0f}% exito")

        # Modelos
        m = identity.get("models", {})
        lines.append(f"\nModelos de IA disponibles: {', '.join(m.get('list', []))}")

        # Tools
        t = identity.get("tools", {})
        if t:
            lines.append(f"\nHerramientas registradas: {t.get('total', 0)}")
            for cat, info in t.get("categories", {}).items():
                lines.append(f"  - {cat}: {info['count']} herramientas")

        # Ejecuciones
        ex = identity.get("executions", {})
        lines.append(f"\nHe ejecutado {ex.get('total', 0)} tareas ({ex.get('successful', 0)} exitosas)")

        # Limites conocidos
        sm = identity.get("self_model", {})
        boundaries = sm.get("knowledge_boundaries", [])
        if boundaries:
            lines.append("\nLimitaciones conocidas:")
            for b in boundaries[:5]:
                lines.append(f"  [{b['severity']}] {b['description']}")

        return "\n".join(lines)

    async def change_project(self, new_project: str):
        """
        Cambia de proyecto: lee CONTEXT.md del nuevo proyecto,
        invalida cache de identidad para que el LLM lo vea.
        """
        old_project = self.current_project
        logger.info(f"Project change: {old_project} → {new_project}")

        self.current_project = new_project
        self._project_context = None

        # Leer CONTEXT.md del nuevo proyecto
        context_path = Path(self._project_root) / "data" / "projects" / new_project / "CONTEXT.md"
        if context_path.exists():
            self._project_context = context_path.read_text(encoding="utf-8").strip()
            logger.info(f"Loaded project context ({len(self._project_context)} chars)")

        # Invalidar cache de identidad para que el LLM reciba el nuevo contexto
        if hasattr(self, "identity_brain"):
            self.identity_brain.invalidate_cache()

        logger.info(f"Project changed: {old_project} → {new_project}")

    def get_status(self) -> Dict:
        """Estado completo del Director — resiliente a componentes faltantes."""
        def _s(attr, method="get_stats"):
            o = getattr(self, attr, None)
            if o is None: return {"unavailable": True}
            try: return getattr(o, method, lambda: {"no_method": method})()
            except Exception: return {"error": True}
        return {
            "identity": self.identity, "current_project": self.current_project,
            "gemas_count": len(self.gemas), "executions": len(self.execution_log),
            "tool_registry": self.tool_registry.get_summary() if hasattr(self, 'tool_registry') else {},
            "gemas": {n: {
                "execution_count": g.execution_count,
                "success_rate": g.success_count / g.execution_count if g.execution_count > 0 else 0,
                "icon": getattr(g, "icon", ""),
                "color": getattr(g, "color", ""),
                "division": getattr(g, "division", ""),
                "personality": getattr(g, "personality", ""),
                "workflow": getattr(g, "workflow", ""),
            } for n, g in self.gemas.items()},
            **{k: _s(*v) if isinstance(v, tuple) else _s(v) for k, v in {
                "sessions": "sessions", "token_budget": ("token_budget", "get_status"),
                "goal_detector": "goal_detector", "dag": "dag", "checkpoints": "checkpoints",
                "recipes": "recipes", "loop_guard": "loop_guard", "approval": "approval",
                "vault": "vault", "risk": "risk", "retry": "retry",
                "gema_host": ("gema_host", "get_status"), "mcp_client": ("mcp_client", "get_status"),
                "llm_gateway": "llm_gateway", "judge": "judge", "autopilot": ("autopilot", "get_status"),
                "self_model": ("self_model", "get_status"),
            }.items()},
        }



    async def shutdown(self):
        """Apaga todos los componentes del Director"""
        logger.info("Shutting down DirectorNexus...")
        await self.worker_manager.stop()
        self.gema_host.shutdown()
        await self.autopilot.stop()
        await self.realtime_hub.stop()
        await self.message_bus.stop()
        if hasattr(self, 'peer_chat'):
            await self.peer_chat.close()
        if hasattr(self, 'cursor') and hasattr(self.cursor, 'store'):
            self.cursor.store.clear_history(older_than_days=7)
        if hasattr(self, 'nexus_trainer'):
            self.nexus_trainer._save_jobs()
        if hasattr(self, 'self_model'):
            self.self_model.save_state()

        logger.info("DirectorNexus shut down")
    
    async def start_background_workers(self):
        """Iniciar background workers + realtime hub + autopilot."""
        context = {
            "director": self,
            "sessions": self.sessions,
            "token_budget": self.token_budget,
            "hooks": self.hooks,
            "compactor": self.compactor,
            "memory_consolidator": self.memory_consolidator,
            "skill_loader": self.skill_loader,
            "connectivity": self.connectivity,
            "memory_health": self.memory_health,
            "execution_log": self.execution_log,
            "nexus_home": str(Path.home() / ".nexus"),
        }

        # Cada step puede ser saltado vía env para diagnostico
        debug = os.environ.get("NEXUS_DEBUG_WORKERS", "")
        if "skip-workers" not in debug and "skip-all" not in debug:
            await self.worker_manager.start(context)
            await asyncio.sleep(0.3)

        if "skip-hub" not in debug and "skip-all" not in debug:
            await self.realtime_hub.start()
            await asyncio.sleep(0.3)

        if "skip-autopilot" not in debug and "skip-all" not in debug:
            try:
                await asyncio.wait_for(self.autopilot.start(), timeout=10)
            except asyncio.TimeoutError:
                logger.error("Autopilot start() timed out — disabled")
            except Exception as e:
                logger.error(f"Autopilot start failed: {e}")
            await asyncio.sleep(0.3)

        logger.info("Background workers started")
    
    async def run_worker(self, worker_name: str) -> Dict:
        """Ejecutar un worker específico on-demand"""
        context = {
            "director": self,
            "sessions": self.sessions,
            "token_budget": self.token_budget,
            "hooks": self.hooks,
            "compactor": self.compactor,
            "memory_consolidator": self.memory_consolidator,
            "skill_loader": self.skill_loader,
            "connectivity": self.connectivity,
            "memory_health": self.memory_health,
            "execution_log": self.execution_log,
            "nexus_home": str(Path.home() / ".nexus"),
        }
        result = await self.worker_manager.run_on_demand(worker_name, context)
        return result.__dict__

    # ── F1: Director Soberano ────────────────────────────────────

    async def _sub_director_handle(self, sd, cmd: Command) -> CommandResult:
        agent_name = sd.select_agent(cmd)
        agent_cmd = Command(
            target=agent_name, action=cmd.action,
            instruction=cmd.instruction, constraints=cmd.constraints,
            priority=cmd.priority, deadline_tokens=cmd.deadline_tokens,
        )
        result = await self.command_dispatcher.dispatch(agent_cmd)
        sd.record_result(result)
        sd.consume_budget(result.tokens_used)
        return result

    async def _gema_handle(self, cmd: Command) -> CommandResult:
        """Handler for gema-* targets - routes to gema_host.execute_gema.
        Tries several name resolution strategies because sub-director agent
        names (e.g. 'gema-coder') don't always match manifest names (e.g. 'code').
        """
        target = cmd.target
        instruction = cmd.instruction if isinstance(cmd.instruction, dict) else {}
        task = instruction.get("task", "") if isinstance(instruction, dict) else str(cmd.instruction)
        context = instruction.get("context", "") if isinstance(instruction, dict) else ""

        candidates = [target]
        if target.startswith("gema-"):
            bare = target[5:]
            candidates.append(bare)
            aliases = {"coder": "code", "ui-designer": "design", "sysadmin": "devops",
                       "monitor": "devops", "web-scraper": "scholar"}
            if bare in aliases:
                candidates.append(aliases[bare])

        last_error = None
        for name in candidates:
            try:
                result = await self.gema_host.execute_gema(
                    name, "execute_task", {"task": task, "context": context}
                )
                if isinstance(result, dict) and "error" not in result:
                    output = result.get("response") or result.get("output") or json.dumps(result, ensure_ascii=False)
                    return CommandResult(
                        command_id=cmd.command_id,
                        status=CommandStatus.COMPLETED,
                        output=str(output),
                        tokens_used=result.get("tokens_used", 0),
                    )
                last_error = result.get("error") if isinstance(result, dict) else str(result)
            except Exception as e:
                last_error = str(e)
                continue

        return CommandResult(
            command_id=cmd.command_id,
            status=CommandStatus.FAILED,
            error=f"Could not activate any gema for target '{target}': {last_error}",
        )

    async def _a2a_execute(self, task: str) -> dict:
        if hasattr(self, 'nexus_execute'):
            return await self.nexus_execute(task)
        return {"error": "Execute not available"}

    def _absorb_to_brain(self, content: str) -> None:
        if hasattr(self, 'hierarchical_memory'):
            self.hierarchical_memory.store(content, tags=["absorbed"], source="code-absorber")

    async def _llm_enhance_call(self, prompt: str) -> str:
        """LLM call for LLMAdapter — uses connectivity layer."""
        try:
            result = await self.connectivity.execute(
                prompt=prompt, engine="ollama", model="qwen2.5-coder:7b",
            )
            return result.data.get("response", "") if result.success else ""
        except Exception as e:
            logger.warning(f"LLM enhance call failed: {e}")
            return ""

    async def _execute_external_agent(self, agent_name: str, command: Command) -> CommandResult:
        agent = self.external_agents.get(agent_name)
        if not agent:
            return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED,
                                 error=f"Unknown external agent: {agent_name}")
        task = command.instruction.get("task", command.instruction.get("prompt", str(command.instruction)))
        if agent_name == "agent-zero":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker", "exec", "agent-zero",
                    "/opt/venv-a0/bin/python", "/zero_helper.py", task,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
                output = stdout.decode("utf-8", errors="replace")
                if proc.returncode == 0:
                    lines = output.splitlines()
                    for line in reversed(lines):
                        clean = line.strip().strip('`').strip()
                        if clean and not clean.startswith('{') and 'Response:' not in clean:
                            return CommandResult(command_id=command.command_id, status=CommandStatus.COMPLETED, output=clean)
                    return CommandResult(command_id=command.command_id, status=CommandStatus.COMPLETED, output=output[:500])
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED, error=stderr.decode()[:500])
            except asyncio.TimeoutError:
                return CommandResult(command_id=command.command_id, status=CommandStatus.TIMEOUT, error="Agent Zero timeout 180s")
            except Exception as e:
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED, error=str(e))
        elif agent_name in ("hermes", "openclaw"):
            meta = agent.metadata or {}
            cli_args = meta.get("cli_args", [])
            cmd_list = [agent.endpoint] + cli_args + ["--prompt", task]
            cmd_str = " ".join(cmd_list)
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd_str, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                output = stdout.decode("utf-8", errors="replace")
                if agent_name == "openclaw":
                    try:
                        data = json.loads(output)
                        texts = [o.get("text", "") for o in data.get("outputs", []) if o.get("text")]
                        output = "\n".join(texts) if texts else output
                    except json.JSONDecodeError:
                        pass
                if proc.returncode == 0:
                    return CommandResult(command_id=command.command_id, status=CommandStatus.COMPLETED, output=output.strip())
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED,
                                     error=output[:300] if output else stderr.decode()[:300])
            except asyncio.TimeoutError:
                return CommandResult(command_id=command.command_id, status=CommandStatus.TIMEOUT, error=f"{agent_name} timeout 120s")
            except Exception as e:
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED, error=str(e))
        elif agent_name.startswith("pc2-"):
            meta = agent.metadata or {}
            cli_command = meta.get("cli_command")
            if cli_command:
                ssh_target = os.environ.get("PC2_SSH_TARGET", "user@remote-host")
                import shlex
                remote_cmd = f'{cli_command} {shlex.quote(task)}'
                ssh_cmd_list = [
                    "ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no",
                    ssh_target, remote_cmd
                ]
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *ssh_cmd_list,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
                    output = stdout.decode("utf-8", errors="replace").strip()
                    if proc.returncode == 0 and output:
                        return CommandResult(command_id=command.command_id, status=CommandStatus.COMPLETED, output=output[:2000])
                    return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED,
                                         error=stderr.decode("utf-8", errors="replace")[:500] or f"Exit code {proc.returncode}")
                except asyncio.TimeoutError:
                    return CommandResult(command_id=command.command_id, status=CommandStatus.TIMEOUT, error=f"{agent_name} SSH timeout 180s")
                except Exception as e:
                    return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED, error=str(e))
            http_endpoint = agent.endpoint
            try:
                async with aiohttp.ClientSession() as session:
                    model = meta.get("model", "qwen2.5-coder:7b")
                    payload = json.dumps({"model": model, "messages": [{"role": "user", "content": task}], "stream": False})
                    async with session.post(http_endpoint, data=payload,
                                            headers={"Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(180)) as resp:
                        data = await resp.json()
                        msg = data.get("message", {})
                        output = msg.get("content", str(data))
                        return CommandResult(command_id=command.command_id, status=CommandStatus.COMPLETED, output=output.strip())
            except Exception as e:
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED, error=str(e))
        # OMA (Open Multi-Agent) orchestrator — hybrid multi-agent execution
        elif agent_name == "oma-orchestrator" or (agent.metadata or {}).get("oma_service"):
            try:
                from src.services.oma_service import OMAService
                
                # Extract agents and goal from command
                goal = command.instruction.get("task", task)
                agents = command.instruction.get("agents", [])
                options = command.instruction.get("options", {})
                
                # Default: if no agents provided, create a basic team
                if not agents:
                    # Use Ollama as default provider
                    agents = [
                        OMAService.create_ollama_agent(
                            "agent-1", system_prompt="You are a helpful assistant."),
                        OMAService.create_ollama_agent(
                            "agent-2", system_prompt="You are a critical reviewer."),
                    ]
                
                result = await OMAService.run_team(goal, agents, options)
                
                if result.get("success"):
                    outputs = []
                    for name, agent_result in result.get("agentResults", {}).items():
                        if agent_result.get("success"):
                            outputs.append(f"[{name}]: {agent_result.get('output', '')[:200]}")
                    output = "\n".join(outputs) if outputs else json.dumps(result)
                    return CommandResult(
                        command_id=command.command_id,
                        status=CommandStatus.COMPLETED,
                        output=output,
                    )
                else:
                    error = result.get("error", "OMA execution failed")
                    return CommandResult(
                        command_id=command.command_id,
                        status=CommandStatus.FAILED,
                        error=error,
                    )
            except Exception as e:
                return CommandResult(
                    command_id=command.command_id,
                    status=CommandStatus.FAILED,
                    error=f"OMA error: {str(e)[:500]}",
                )
        
        # Generic CLI handler for any CLI agent with endpoint
        elif agent.protocol == "cli" and agent.endpoint:
            try:
                import shlex
                proc = await asyncio.create_subprocess_exec(
                    agent.endpoint, task,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
                output = stdout.decode("utf-8", errors="replace")
                if proc.returncode == 0:
                    return CommandResult(command_id=command.command_id, status=CommandStatus.COMPLETED, output=output.strip()[:500])
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED,
                                     error=output[:300] if output else stderr.decode()[:300])
            except asyncio.TimeoutError:
                return CommandResult(command_id=command.command_id, status=CommandStatus.TIMEOUT, error=f"{agent_name} timeout 120s")
            except Exception as e:
                return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED, error=str(e))
        return CommandResult(command_id=command.command_id, status=CommandStatus.FAILED,
                             error=f"No handler for {agent_name}")

    async def nexus_execute(self, task: str) -> dict:
        # 1. Learning check
        if self.learning_loop.has_gap(task):
            learn_result = await self.learning_loop.learn(task)
            if not learn_result.found:
                logger.warning(f"Knowledge gap detected, no learning source found for: {task[:80]}")

        # 2. Decompose (deterministic)
        commands = self.decision_engine.decompose(task)

        # 2b. Optional LLM enhancement (engine has final say)
        if self.llm_adapter.available:
            commands = await self.llm_adapter.enhance_decomposition(task, commands)

        # 3. Dispatch
        results = await self.command_dispatcher.dispatch_batch(commands, max_parallel=3)

        # 4. Evaluate
        verdicts = []
        for cmd, result in zip(commands, results):
            verdict = self.decision_engine.evaluate(
                output=result.output, exit_code=0 if result.status == CommandStatus.COMPLETED else 1,
                error=result.error,
            )
            # Optional LLM second opinion
            if self.llm_adapter.available:
                verdict = await self.llm_adapter.judge(result.output, task, verdict)
            verdicts.append(verdict)

        # 5. Synthesize
        success = all(v.passed for v in verdicts)
        summary = await self.llm_adapter.synthesize(
            [r.to_dict() for r in results], task
        ) if self.llm_adapter.available else f"{'OK' if success else 'FAILED'}: {len(commands)} commands"

        return {
            "task": task,
            "success": success,
            "commands": len(commands),
            "summary": summary,
            "results": [r.to_dict() for r in results],
            "verdicts": [{"passed": v.passed, "reason": v.reason} for v in verdicts],
        }
