"""
CapabilityDiscovery - Auto-discovery pipeline for DirectorNexus

Discovers all available capabilities from multiple sources:
1. Filesystem scan (gema manifests, skills, tools)
2. Runtime discovery (Ollama models, system capabilities, network nodes)
3. Historical learning (execution logs, success rates)

Output: CapabilityMap with dynamic self-description.
"""

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class GemaCapability:
    """Dynamic gema capability record"""
    name: str
    tags: List[str]
    description: str
    model: str
    category: str = "general"
    activation_events: List[str] = field(default_factory=list)
    semantic_keywords: List[str] = field(default_factory=list)
    parallel_capable: bool = True
    available: bool = True
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    execution_count: int = 0
    source: str = "hardcoded"  # hardcoded, manifest, discovered


@dataclass
class ToolCapability:
    """Dynamic tool capability record"""
    name: str
    description: str
    tags: List[str]
    requires: List[str] = field(default_factory=list)
    available: bool = True
    call_count: int = 0


@dataclass
class ModelCapability:
    """Dynamic model capability record"""
    name: str
    ollama_name: str
    capabilities: List[str]
    max_context: int = 4096
    is_local: bool = True
    available: bool = False
    size_mb: float = 0.0


@dataclass
class SkillCapability:
    """Dynamic skill capability record"""
    name: str
    category: str
    description: str
    tags: List[str]
    available: bool = True
    usage_count: int = 0


@dataclass
class SystemCapability:
    """System-level capability record"""
    has_gpu: bool = False
    gpu_info: str = ""
    total_ram_gb: float = 0.0
    available_ram_gb: float = 0.0
    cpu_cores: int = 0
    ollama_available: bool = False
    ollama_models: List[str] = field(default_factory=list)


@dataclass
class NodeCapability:
    """Remote node capability record"""
    node_id: str
    status: str = "unknown"
    capabilities: List[str] = field(default_factory=list)
    gpu_info: str = ""


@dataclass
class CapabilityMap:
    """Complete capability map of DirectorNexus"""
    gemas: Dict[str, GemaCapability] = field(default_factory=dict)
    tools: Dict[str, ToolCapability] = field(default_factory=dict)
    models: Dict[str, ModelCapability] = field(default_factory=dict)
    skills: Dict[str, SkillCapability] = field(default_factory=dict)
    system: Optional[SystemCapability] = None
    nodes: Dict[str, NodeCapability] = field(default_factory=dict)
    self_description: str = ""
    last_updated: str = ""
    changes_since_last: List[str] = field(default_factory=list)


class CapabilityDiscovery:
    """
    Multi-source capability discovery pipeline.
    
    Scans filesystem, runtime environment, and historical data
    to build a complete, dynamic CapabilityMap.
    """

    def __init__(
        self,
        project_root: str = None,
        ollama_client = None,
        execution_log: List[Dict] = None,
    ):
        self.project_root = Path(project_root) if project_root else Path(__file__).parent.parent.parent
        self.ollama_client = ollama_client
        self.execution_log = execution_log or []
        self.current_map: Optional[CapabilityMap] = None
        self._previous_map: Optional[CapabilityMap] = None

    async def full_discovery(self) -> CapabilityMap:
        """Execute complete discovery from all sources"""
        cap_map = CapabilityMap()
        cap_map.last_updated = datetime.now().isoformat()

        logger.info("Starting full capability discovery...")

        # 1. Filesystem scan
        cap_map.gemas = await self._scan_gema_capabilities()
        cap_map.skills = await self._scan_skill_capabilities()
        cap_map.tools = await self._scan_tool_capabilities()

        # 2. Runtime discovery
        cap_map.models = await self._discover_ollama_models()
        cap_map.system = await self._discover_system_capabilities()
        cap_map.nodes = await self._discover_network_nodes()

        # 3. Enrich with historical data
        cap_map = self._enrich_with_history(cap_map)

        # 4. Generate self-description
        cap_map.self_description = self._generate_self_description(cap_map)

        # 5. Detect changes from previous map
        if self._previous_map:
            cap_map.changes_since_last = self._detect_changes(self._previous_map, cap_map)

        self._previous_map = self.current_map
        self.current_map = cap_map

        logger.info(
            f"Capability discovery complete: "
            f"{len(cap_map.gemas)} gemas, {len(cap_map.models)} models, "
            f"{len(cap_map.skills)} skills, {len(cap_map.tools)} tools"
        )

        return cap_map

    async def _scan_gema_capabilities(self) -> Dict[str, GemaCapability]:
        """Scan gema manifests and hardcoded definitions"""
        gemas: Dict[str, GemaCapability] = {}

        # 1. Scan filesystem for gema.json manifests
        manifest_dirs = [
            self.project_root / "data" / "gemas",
            self.project_root / "src" / "gemas",
        ]

        for manifest_dir in manifest_dirs:
            if not manifest_dir.exists():
                continue
            for manifest_file in manifest_dir.glob("*.json"):
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    gema = GemaCapability(
                        name=data.get("name", manifest_file.stem),
                        tags=data.get("semanticKeywords", []),
                        description=data.get("description", ""),
                        model=data.get("model", ""),
                        category=data.get("category", "general"),
                        activation_events=data.get("activationEvents", []),
                        semantic_keywords=data.get("semanticKeywords", []),
                        parallel_capable=data.get("parallelCapable", True),
                        source="manifest",
                    )
                    gemas[gema.name] = gema
                    logger.debug(f"Loaded gema manifest: {gema.name}")
                except Exception as e:
                    logger.error(f"Failed to load gema manifest {manifest_file}: {e}")

        # 2. Load hardcoded gemas from GemaHost manifests
        try:
            from src.core.gema_host import GemaHost
            host = GemaHost(project_root=str(self.project_root))
            host.initialize()
            for name, manifest in host._manifests.items():
                if name not in gemas:
                    gema = GemaCapability(
                        name=manifest.name,
                        tags=manifest.semantic_keywords,
                        description=manifest.description,
                        model=manifest.model,
                        category=manifest.category,
                        activation_events=manifest.activation_events,
                        semantic_keywords=manifest.semantic_keywords,
                        parallel_capable=manifest.parallel_capable,
                        source="gema_host",
                    )
                    gemas[gema.name] = gema
        except Exception as e:
            logger.warning(f"GemaHost manifest scan failed: {e}")

        # 3. Fallback: hardcoded gema definitions (same structure as director.py)
        if not gemas:
            hardcoded_gemas = [
                ("ayuda",    ["help", "ayuda", "tutorial", "guide", "onboarding", "capacidades"], "Guia reactiva del sistema", "qwen2.5-coder:7b"),
                ("director", ["leadership", "orchestration", "planning"], "Orquestacion y liderazgo", "qwen2.5-coder:7b"),
                ("code",     ["programming", "code-review", "refactoring", "handoff", "delegation", "compile", "sandbox"], "Programacion, ejecucion y sandbox", "qwen2.5-coder:7b"),
                ("scholar",  ["research", "learning", "web-search"], "Investigacion y aprendizaje", "deepseek-r1:8b"),
                ("architect", ["architecture", "design", "infrastructure"], "Diseno de sistemas", "qwen2.5-coder:7b"),
                ("creative", ["creative", "writing", "content"], "Contenido creativo", "qwen2.5-coder:7b"),
                ("sage",     ["memory", "persistence", "learning"], "Persistencia y memoria", "deepseek-r1:8b"),
                ("analyst",  ["analysis", "data", "metrics"], "Analisis de datos", "nemotron-3-nano:4b"),
                ("debugger", ["debugging", "troubleshooting", "error-handling"], "Debugging", "deepseek-r1:8b"),
                ("optimizer",["optimization", "performance", "tuning"], "Optimizacion", "qwen2.5-coder:7b"),
                ("tester",   ["testing", "qa", "validation"], "Testing y QA", "qwen2.5-coder:7b"),
                ("security", ["security", "compliance", "protection"], "Seguridad", "deepseek-r1:8b"),
                ("devops",   ["devops", "deployment", "infrastructure"], "DevOps", "qwen2.5-coder:7b"),
                ("trainer",  ["training", "education", "teaching"], "Entrenamiento", "qwen2.5-coder:7b"),
                ("biblioteca",["organization", "knowledge", "indexing"], "Organizacion de conocimiento", "deepseek-r1:8b"),
                ("vision",   ["screenshot", "screen-control", "pc-control", "mouse", "keyboard"], "Control visual de PC", "qwen2.5vl:7b"),
                ("opencode", ["opencode", "cli-agent", "code-execution", "engineering", "tools", "scripting"], "Agente CLI, scripting y automatizacion", "qwen2.5-coder:7b"),
                ("design",   ["design", "ui", "ux", "multimedia", "video", "scene"], "Diseno multimedia", "qwen2.5-coder:7b"),
                ("music",    ["music", "audio", "sound", "voice", "tts", "stt"], "Audio, voz y musica", "qwen2.5-coder:7b"),
                ("prompter", ["prompt", "token", "optimization", "compression"], "Optimizacion de prompts y tokens", "qwen2.5-coder:7b"),
                ("producer", ["schedule", "task", "automation", "rcon", "server", "cron", "backup"], "Automatizacion, cron y servidores", "qwen2.5-coder:7b"),
                ("engineer", ["engineering", "tools", "automation", "scripting", "cli", "build"], "Ingenieria de herramientas y build", "qwen2.5-coder:7b"),
                ("codex",    ["codex", "delegation", "compilation", "sandbox"], "Delegacion de codigo y sandbox", "qwen2.5-coder:7b"),
                ("verifier", ["verification", "qa", "validation", "review", "quality", "audit"], "Verificacion adversarial de codigo y artefactos", "deepseek-r1:8b"),
            ]
            for name, tags, desc, model in hardcoded_gemas:
                gemas[name] = GemaCapability(
                    name=name,
                    tags=tags,
                    description=desc,
                    model=model,
                    source="hardcoded",
                )

        logger.info(f"Discovered {len(gemas)} gema capabilities")
        return gemas

    async def _scan_skill_capabilities(self) -> Dict[str, SkillCapability]:
        """Scan skill files for capabilities"""
        skills: Dict[str, SkillCapability] = {}
        skills_dir = self.project_root / "src" / "skills" / "hub"

        if not skills_dir.exists():
            logger.warning(f"Skills directory not found: {skills_dir}")
            return skills

        for skill_file in skills_dir.glob("*.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                name = skill_file.stem

                # Extract metadata from first 20 lines
                description = ""
                category = "general"
                tags = []

                for line in content.split("\n")[:20]:
                    line_lower = line.lower()
                    if line.startswith("# ") and not description:
                        description = line[2:].strip()
                    if "category:" in line_lower:
                        category = line.split(":")[1].strip()
                    if "tags:" in line_lower:
                        tags = [t.strip() for t in line.split(":")[1].split(",")]

                skills[name] = SkillCapability(
                    name=name,
                    category=category,
                    description=description or f"Skill: {name}",
                    tags=tags,
                )
            except Exception as e:
                logger.error(f"Failed to scan skill {skill_file}: {e}")

        logger.info(f"Discovered {len(skills)} skill capabilities")
        return skills

    async def _scan_tool_capabilities(self) -> Dict[str, ToolCapability]:
        """Scan registered tools from AIToolsRegistry"""
        tools: Dict[str, ToolCapability] = {}

        try:
            from src.core.ai_tools import AIToolsRegistry
            registry = AIToolsRegistry()

            for tool in registry.tools.values():
                tools[tool.name] = ToolCapability(
                    name=tool.name,
                    description=f"AI tool: {tool.role}",
                    tags=tool.tags,
                    call_count=tool.call_count,
                )

            # Also get auto-registered tools
            for reg_tool in registry.get_registered_tools():
                tools[reg_tool["name"]] = ToolCapability(
                    name=reg_tool["name"],
                    description=reg_tool["description"],
                    tags=reg_tool.get("tags", []),
                    requires=reg_tool.get("requires", []),
                )
        except Exception as e:
            logger.warning(f"Tool registry scan failed: {e}")

        logger.info(f"Discovered {len(tools)} tool capabilities")
        return tools

    async def _discover_ollama_models(self) -> Dict[str, ModelCapability]:
        """Discover available Ollama models"""
        models: Dict[str, ModelCapability] = {}

        # Canonical model registry definitions
        canonical_models = {
            "qwen_coder": {
                "ollama_name": "qwen2.5-coder:7b",
                "capabilities": ["code", "programming", "debug"],
                "max_context": 8192,
            },
            "deepseek_reason": {
                "ollama_name": "deepseek-r1:8b",
                "capabilities": ["reasoning", "analysis", "planning"],
                "max_context": 8192,
            },
            "nemotron_fast": {
                "ollama_name": "nemotron-3-nano:4b",
                "capabilities": ["fast", "simple", "summary"],
                "max_context": 4096,
            },
            "qwen_vision": {
                "ollama_name": "qwen2.5vl:7b",
                "capabilities": ["vision", "image", "screenshot"],
                "max_context": 4096,
            },
            "gemma_creative": {
                "ollama_name": "qwen2.5-coder:7b",
                "capabilities": ["creative", "writing", "generation"],
                "max_context": 8192,
            },
            "scholar_research": {
                "ollama_name": "deepseek-r1:8b",
                "capabilities": ["research", "search", "analysis"],
                "max_context": 8192,
            },
        }

        for name, info in canonical_models.items():
            models[name] = ModelCapability(
                name=name,
                ollama_name=info["ollama_name"],
                capabilities=info["capabilities"],
                max_context=info["max_context"],
            )

        # Check which are actually available via Ollama
        if self.ollama_client:
            try:
                available = await self.ollama_client.list_models()
                available_names = {m.get("name", "") for m in available}

                for model in models.values():
                    # Check if ollama_name or base name is available
                    base_name = model.ollama_name.split(":")[0]
                    model.available = any(
                        model.ollama_name in available_names or
                        av.startswith(base_name)
                        for av in available_names
                    )
                    if model.available:
                        # Get size info
                        for m in available:
                            if m.get("name", "").startswith(base_name):
                                model.size_mb = m.get("size", 0) / (1024 * 1024)
                                break

                logger.info(
                    f"Ollama model discovery: "
                    f"{sum(1 for m in models.values() if m.available)}/{len(models)} available"
                )
            except Exception as e:
                logger.warning(f"Ollama model discovery failed: {e}")

        return models

    async def _discover_system_capabilities(self) -> SystemCapability:
        """Discover system-level capabilities"""
        sys_cap = SystemCapability()

        # CPU info
        try:
            import psutil
            sys_cap.cpu_cores = psutil.cpu_count(logical=True)
            sys_cap.total_ram_gb = psutil.virtual_memory().total / (1024 ** 3)
            sys_cap.available_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
        except ImportError:
            logger.warning("psutil not available, skipping system discovery")
            sys_cap.cpu_cores = 1
            sys_cap.total_ram_gb = 0.0
            sys_cap.available_ram_gb = 0.0

        # GPU info (NVIDIA + AMD ROCm)
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                sys_cap.has_gpu = True
                sys_cap.gpu_info = f"NVIDIA: {result.stdout.strip()}"
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        # AMD GPU detection via rocm-smi
        if not sys_cap.has_gpu:
            try:
                result = subprocess.run(
                    ["rocm-smi", "--showproductname"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    sys_cap.has_gpu = True
                    sys_cap.gpu_info = f"AMD: {result.stdout.strip()[:200]}"
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass

        # Ollama availability
        if self.ollama_client:
            try:
                sys_cap.ollama_available = await self.ollama_client.is_available()
                if sys_cap.ollama_available:
                    models = await self.ollama_client.list_models()
                    sys_cap.ollama_models = [m.get("name", "") for m in models]
            except Exception:
                sys_cap.ollama_available = False

        logger.info(
            f"System discovery: CPU={sys_cap.cpu_cores} cores, "
            f"RAM={sys_cap.total_ram_gb:.1f}GB, GPU={sys_cap.has_gpu}, "
            f"Ollama={sys_cap.ollama_available}"
        )

        return sys_cap

    async def _discover_network_nodes(self) -> Dict[str, NodeCapability]:
        """Discover network nodes (PC2, etc.)"""
        nodes: Dict[str, NodeCapability] = {}

        # Known nodes to check
        known_nodes = {
            "remote": {"host": os.environ.get("REMOTE_NODE_IP", "remote-host"), "port": int(os.environ.get("REMOTE_NODE_PORT", "8000"))},
        }

        for node_id, info in known_nodes.items():
            node = NodeCapability(node_id=node_id)
            try:
                import httpx
                async with httpx.AsyncClient(timeout=3.0) as client:
                    r = await client.get(f"http://{info['host']}:{info['port']}/health")
                    if r.status_code == 200:
                        node.status = "online"
                        node.capabilities = ["gpu", "remote_execution"]
                        data = r.json()
                        node.gpu_info = data.get("gpu", "")
            except Exception:
                node.status = "offline"
            nodes[node_id] = node

        logger.info(f"Network discovery: {len(nodes)} nodes checked")
        return nodes

    def _enrich_with_history(self, cap_map: CapabilityMap) -> CapabilityMap:
        """Enrich capability map with historical execution data"""
        if not self.execution_log:
            return cap_map

        # Calculate success rates and latencies per gema
        gema_stats: Dict[str, Dict] = {}
        for entry in self.execution_log:
            for gem_name in entry.get("gems", []):
                if gem_name not in gema_stats:
                    gema_stats[gem_name] = {
                        "total": 0,
                        "success": 0,
                        "latency_total": 0.0,
                    }
                gema_stats[gem_name]["total"] += 1
                if entry.get("success"):
                    gema_stats[gem_name]["success"] += 1
                gema_stats[gem_name]["latency_total"] += entry.get("duration_ms", 0)

        # Update gema capabilities with stats
        for name, stats in gema_stats.items():
            if name in cap_map.gemas:
                gema = cap_map.gemas[name]
                gema.execution_count = stats["total"]
                gema.success_rate = stats["success"] / stats["total"] if stats["total"] > 0 else 0.0
                gema.avg_latency_ms = stats["latency_total"] / stats["total"] if stats["total"] > 0 else 0.0

        return cap_map

    def _generate_self_description(self, cap_map: CapabilityMap) -> str:
        """Generate dynamic self-description for system prompt"""
        lines = [
            "# DirectorNexus - Dynamic Capability Report",
            f"Generated: {cap_map.last_updated}",
            "",
            f"## Available Gemas ({len(cap_map.gemas)})",
        ]

        # Group gemas by category
        categories: Dict[str, List[GemaCapability]] = {}
        for gema in cap_map.gemas.values():
            cat = gema.category
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(gema)

        for category, gemas in sorted(categories.items()):
            lines.append(f"\n### {category}")
            for gema in sorted(gemas, key=lambda g: g.name):
                perf = ""
                if gema.execution_count > 0:
                    perf = f" (success: {gema.success_rate:.0%}, avg: {gema.avg_latency_ms:.0f}ms)"
                lines.append(f"- **{gema.name}**: {gema.description}{perf}")

        lines.append(f"\n## Available Models ({sum(1 for m in cap_map.models.values() if m.available)})")
        for model in cap_map.models.values():
            status = "AVAILABLE" if model.available else "unavailable"
            lines.append(f"- {model.name} ({model.ollama_name}): {', '.join(model.capabilities)} [{status}]")

        if cap_map.system:
            lines.append("\n## System Capabilities")
            lines.append(f"- CPU: {cap_map.system.cpu_cores} cores")
            lines.append(f"- RAM: {cap_map.system.available_ram_gb:.1f}GB available / {cap_map.system.total_ram_gb:.1f}GB total")
            lines.append(f"- GPU: {'Yes' if cap_map.system.has_gpu else 'No'} {cap_map.system.gpu_info}")
            lines.append(f"- Ollama: {'Available' if cap_map.system.ollama_available else 'Not available'}")

        if cap_map.nodes:
            lines.append("\n## Network Nodes")
            for node in cap_map.nodes.values():
                lines.append(f"- {node.node_id}: {node.status} ({', '.join(node.capabilities) if node.capabilities else 'no capabilities'})")

        lines.append(f"\n## Skills ({len(cap_map.skills)})")
        lines.append(f"- {len(cap_map.skills)} skills available in catalog")

        lines.append(f"\n## Tools ({len(cap_map.tools)})")
        lines.append(f"- {len(cap_map.tools)} AI tools registered")

        return "\n".join(lines)

    def _detect_changes(self, old: CapabilityMap, new: CapabilityMap) -> List[str]:
        """Detect changes between two capability maps"""
        changes = []

        # New gemas
        new_gemas = set(new.gemas.keys()) - set(old.gemas.keys())
        for name in new_gemas:
            changes.append(f"New gema discovered: {name}")

        # Removed gemas
        removed_gemas = set(old.gemas.keys()) - set(new.gemas.keys())
        for name in removed_gemas:
            changes.append(f"Gema no longer available: {name}")

        # Model availability changes
        for name, model in new.models.items():
            old_model = old.models.get(name)
            if old_model and old_model.available != model.available:
                status = "now available" if model.available else "now unavailable"
                changes.append(f"Model {name} {status}")

        # Node status changes
        for node_id, node in new.nodes.items():
            old_node = old.nodes.get(node_id)
            if old_node and old_node.status != node.status:
                changes.append(f"Node {node_id} status changed: {old_node.status} -> {node.status}")

        return changes

    async def periodic_refresh(self, interval_hours: float = 24.0):
        """Periodic automatic refresh of capabilities"""
        while True:
            await asyncio.sleep(interval_hours * 3600)
            logger.info("Periodic capability refresh triggered")
            new_map = await self.full_discovery()
            if new_map.changes_since_last:
                logger.info(f"Capability changes detected: {new_map.changes_since_last}")

    def get_gema_for_task(self, task: str) -> Optional[str]:
        """Find best gema for a task using semantic keywords"""
        task_lower = task.lower()
        best_match = None
        best_score = 0

        for name, gema in self.current_map.gemas.items():
            if not gema.available:
                continue

            # Score based on tag matches
            score = sum(1 for tag in gema.tags if tag.lower() in task_lower)
            # Score based on semantic keyword matches
            score += sum(1 for kw in gema.semantic_keywords if kw.lower() in task_lower)
            # Score based on activation event matches
            score += sum(3 for event in gema.activation_events if event.split(":")[-1].lower() in task_lower)

            # Bonus for historical success rate
            if gema.execution_count > 0:
                score *= (0.5 + gema.success_rate * 0.5)

            if score > best_score:
                best_score = score
                best_match = name

        return best_match if best_score > 0 else None

    def get_available_models_for_capability(self, capability: str) -> List[str]:
        """Get available models that support a specific capability"""
        return [
            name for name, model in self.current_map.models.items()
            if model.available and capability in model.capabilities
        ]

    def get_status(self) -> Dict:
        """Get discovery status summary"""
        if not self.current_map:
            return {"status": "not_discovered"}

        return {
            "last_updated": self.current_map.last_updated,
            "gema_count": len(self.current_map.gemas),
            "available_gemas": sum(1 for g in self.current_map.gemas.values() if g.available),
            "model_count": len(self.current_map.models),
            "available_models": sum(1 for m in self.current_map.models.values() if m.available),
            "skill_count": len(self.current_map.skills),
            "tool_count": len(self.current_map.tools),
            "node_count": len(self.current_map.nodes),
            "online_nodes": sum(1 for n in self.current_map.nodes.values() if n.status == "online"),
            "changes_since_last": self.current_map.changes_since_last,
        }
