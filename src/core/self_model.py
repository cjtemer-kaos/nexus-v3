"""
SelfModelEngine - DirectorNexus self-awareness engine

DirectorNexus knows WHAT it is, WHAT it can do, and WHAT it cannot do.

Components:
1. CapabilityRegistry - Auto-discovered capabilities (gemas, tools, models, skills)
2. PerformanceProfiler - Success rates, latency, token efficiency per capability
3. KnowledgeBoundaryDetector - Detects what DirectorNexus DOESN'T know
4. SelfDescriptionGenerator - Generates dynamic system prompt from self-model

This replaces hardcoded IDENTITY and static keyword routing.
"""

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from src.core.capability_discovery import (
    CapabilityDiscovery,
    CapabilityMap,
)
from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class PerformanceProfile:
    """Performance profile for a capability"""
    capability_name: str
    capability_type: str  # gema, tool, model
    success_rate: float = 0.0
    avg_latency_ms: float = 0.0
    total_executions: int = 0
    token_efficiency: float = 0.0  # tokens per successful execution
    best_task_types: List[str] = field(default_factory=list)
    worst_task_types: List[str] = field(default_factory=list)
    last_updated: str = ""


@dataclass
class KnowledgeBoundary:
    """Represents a known limitation"""
    boundary_type: str  # missing_gema, missing_model, missing_skill, failed_pattern
    description: str
    affected_tasks: List[str] = field(default_factory=list)
    severity: str = "medium"  # low, medium, high, critical
    discovered_at: str = ""
    workaround: str = ""


@dataclass
class RoutingRule:
    """Learned routing rule"""
    task_pattern: str
    recommended_gema: str
    confidence: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    created_at: str = ""
    last_used: str = ""


class SelfModelEngine:
    """
    DirectorNexus self-model engine.
    
    Maintains awareness of:
    - Available capabilities (gemas, tools, models, skills)
    - Performance profiles per capability
    - Knowledge boundaries (what it cannot do)
    - Learned routing rules
    """

    def __init__(
        self,
        project_root: str = None,
        ollama_client = None,
        execution_log: List[Dict] = None,
        storage_path: str = None,
    ):
        self.project_root = project_root
        self.ollama_client = ollama_client
        self.execution_log = execution_log or []
        self.storage_path = Path(storage_path) if storage_path else None

        # Core components
        self.discovery = CapabilityDiscovery(
            project_root=project_root,
            ollama_client=ollama_client,
            execution_log=self.execution_log,
        )
        self.capability_map: Optional[CapabilityMap] = None

        # Performance profiles
        self.performance_profiles: Dict[str, PerformanceProfile] = {}
        self._perf_lock = threading.Lock()

        # Knowledge boundaries
        self.knowledge_boundaries: List[KnowledgeBoundary] = []
        self._boundary_lock = threading.Lock()

        # Learned routing rules
        self.routing_rules: Dict[str, RoutingRule] = {}
        self._routing_lock = threading.Lock()

        # Identity cache
        self._identity_cache: Optional[Dict] = None
        self._self_description_cache: str = ""
        self._cache_timestamp: str = ""

        # Load persisted state if available
        if self.storage_path:
            self._load_state()

    async def initialize(self) -> CapabilityMap:
        """Initialize self-model with full discovery"""
        logger.info("Initializing SelfModelEngine...")
        self.capability_map = await self.discovery.full_discovery()
        self._build_performance_profiles()
        self._detect_knowledge_boundaries()
        self._learn_routing_rules()
        self._generate_identity()
        logger.info("SelfModelEngine initialized")
        return self.capability_map

    async def refresh(self) -> CapabilityMap:
        """Refresh capabilities and update self-model"""
        logger.info("Refreshing self-model...")
        self.capability_map = await self.discovery.full_discovery()
        self._build_performance_profiles()
        self._detect_knowledge_boundaries()
        self._invalidate_cache()
        logger.info("Self-model refreshed")
        return self.capability_map

    def _build_performance_profiles(self):
        """Build performance profiles from execution log"""
        if not self.execution_log:
            return

        # Aggregate stats per gema
        gema_stats: Dict[str, Dict] = {}
        for entry in self.execution_log:
            for gem_name in entry.get("gems", []):
                if gem_name not in gema_stats:
                    gema_stats[gem_name] = {
                        "total": 0,
                        "success": 0,
                        "latency_total": 0.0,
                        "task_types": {},
                    }
                gema_stats[gem_name]["total"] += 1
                if entry.get("success"):
                    gema_stats[gem_name]["success"] += 1
                gema_stats[gem_name]["latency_total"] += entry.get("duration_ms", 0)

                # Track task types
                task = entry.get("task", "").lower()
                task_type = self._classify_task_type(task)
                if task_type not in gema_stats[gem_name]["task_types"]:
                    gema_stats[gem_name]["task_types"][task_type] = {"success": 0, "total": 0}
                gema_stats[gem_name]["task_types"][task_type]["total"] += 1
                if entry.get("success"):
                    gema_stats[gem_name]["task_types"][task_type]["success"] += 1

        with self._perf_lock:
            for gem_name, stats in gema_stats.items():
                total = stats["total"]
                success_rate = stats["success"] / total if total > 0 else 0.0
                avg_latency = stats["latency_total"] / total if total > 0 else 0.0

                # Find best and worst task types
                task_types = stats["task_types"]
                best_types = sorted(
                    task_types.keys(),
                    key=lambda t: task_types[t]["success"] / max(task_types[t]["total"], 1),
                    reverse=True,
                )[:3]
                worst_types = sorted(
                    task_types.keys(),
                    key=lambda t: task_types[t]["success"] / max(task_types[t]["total"], 1),
                )[:3]

                self.performance_profiles[gem_name] = PerformanceProfile(
                    capability_name=gem_name,
                    capability_type="gema",
                    success_rate=success_rate,
                    avg_latency_ms=avg_latency,
                    total_executions=total,
                    best_task_types=best_types,
                    worst_task_types=worst_types,
                    last_updated=datetime.now().isoformat(),
                )

        logger.info(f"Built {len(self.performance_profiles)} performance profiles")

    def _detect_knowledge_boundaries(self):
        """Detect what DirectorNexus cannot do"""
        boundaries = []

        # 1. Missing models
        if self.capability_map:
            unavailable_models = [
                m for m in self.capability_map.models.values()
                if not m.available
            ]
            for model in unavailable_models:
                boundaries.append(KnowledgeBoundary(
                    boundary_type="missing_model",
                    description=f"Model {model.name} ({model.ollama_name}) not available in Ollama",
                    affected_tasks=model.capabilities,
                    severity="medium",
                    discovered_at=datetime.now().isoformat(),
                    workaround="Use alternative model with similar capabilities",
                ))

            # 2. Gemas with low success rates
            for name, profile in self.performance_profiles.items():
                if profile.total_executions >= 5 and profile.success_rate < 0.5:
                    boundaries.append(KnowledgeBoundary(
                        boundary_type="low_success_gema",
                        description=f"Gema {name} has low success rate: {profile.success_rate:.0%}",
                        worst_task_types=profile.worst_task_types,
                        severity="high",
                        discovered_at=datetime.now().isoformat(),
                        workaround="Consider using different gema for these task types",
                    ))

            # 3. Offline nodes
            for node in self.capability_map.nodes.values():
                if node.status == "offline":
                    boundaries.append(KnowledgeBoundary(
                        boundary_type="offline_node",
                        description=f"Node {node.node_id} is offline",
                        severity="low",
                        discovered_at=datetime.now().isoformat(),
                        workaround="Tasks requiring GPU must wait or use local models",
                    ))

        with self._boundary_lock:
            self.knowledge_boundaries = boundaries

        logger.info(f"Detected {len(boundaries)} knowledge boundaries")

    def _learn_routing_rules(self):
        """Learn routing rules from execution history"""
        if not self.execution_log:
            return

        # Group executions by task pattern
        pattern_stats: Dict[str, Dict[str, int]] = {}
        for entry in self.execution_log:
            task = entry.get("task", "").lower()
            task_type = self._classify_task_type(task)
            gemas = entry.get("gems", [])
            success = entry.get("success", False)

            if task_type not in pattern_stats:
                pattern_stats[task_type] = {}

            for gem_name in gemas:
                if gem_name not in pattern_stats[task_type]:
                    pattern_stats[task_type][gem_name] = {"success": 0, "total": 0}
                pattern_stats[task_type][gem_name]["total"] += 1
                if success:
                    pattern_stats[task_type][gem_name]["success"] += 1

        with self._routing_lock:
            for task_type, gemas in pattern_stats.items():
                # Find best gema for this task type
                best_gema = None
                best_rate = 0
                best_total = 0

                for gem_name, stats in gemas.items():
                    rate = stats["success"] / max(stats["total"], 1)
                    if stats["total"] >= 3 and rate > best_rate:
                        best_rate = rate
                        best_gema = gem_name
                        best_total = stats["total"]

                if best_gema:
                    self.routing_rules[task_type] = RoutingRule(
                        task_pattern=task_type,
                        recommended_gema=best_gema,
                        confidence=best_rate,
                        success_count=sum(s["success"] for s in gemas.values()),
                        failure_count=sum(s["total"] - s["success"] for s in gemas.values()),
                        created_at=datetime.now().isoformat(),
                        last_used=datetime.now().isoformat(),
                    )

        logger.info(f"Learned {len(self.routing_rules)} routing rules")

    def _classify_task_type(self, task: str) -> str:
        """Simple task type classification"""
        task_lower = task.lower()

        if any(k in task_lower for k in ["code", "python", "javascript", "script", "program"]):
            return "coding"
        if any(k in task_lower for k in ["debug", "error", "bug", "fix"]):
            return "debugging"
        if any(k in task_lower for k in ["research", "search", "investigate", "learn"]):
            return "research"
        if any(k in task_lower for k in ["test", "qa", "validate"]):
            return "testing"
        if any(k in task_lower for k in ["design", "architecture", "plan"]):
            return "design"
        if any(k in task_lower for k in ["deploy", "docker", "devops", "server"]):
            return "devops"
        if any(k in task_lower for k in ["security", "audit", "vulnerability"]):
            return "security"
        if any(k in task_lower for k in ["optimize", "performance", "speed"]):
            return "optimization"
        if any(k in task_lower for k in ["write", "content", "creative"]):
            return "creative"
        if any(k in task_lower for k in ["analyze", "data", "metrics"]):
            return "analysis"

        return "general"

    def _generate_identity(self):
        """Generate dynamic identity from self-model"""
        if not self.capability_map:
            return

        available_gemas = [
            g for g in self.capability_map.gemas.values()
            if g.available
        ]
        available_models = [
            m for m in self.capability_map.models.values()
            if m.available
        ]

        self._identity_cache = {
            "name": "DirectorNexus",
            "version": "3.0",
            "role": "Cerebro central autonomo del ecosistema NEXUS IA",
            "function": "Orquestar gemas, herramientas y modelos para resolver tareas de forma autonoma",
            "architecture": "Self-Model + Three-Loop Self-Improvement",
            "available_gema_count": len(available_gemas),
            "available_model_count": len(available_models),
            "capabilities_summary": self._generate_capabilities_summary(),
            "generated_at": datetime.now().isoformat(),
        }

        self._self_description_cache = self.capability_map.self_description
        self._cache_timestamp = datetime.now().isoformat()

    def _generate_capabilities_summary(self) -> str:
        """Generate concise capabilities summary"""
        if not self.capability_map:
            return "No capabilities discovered yet"

        lines = []

        # Top performing gemas
        top_gemas = sorted(
            [g for g in self.capability_map.gemas.values() if g.execution_count > 0],
            key=lambda g: g.success_rate,
            reverse=True,
        )[:5]

        if top_gemas:
            lines.append("Top performing gemas:")
            for gema in top_gemas:
                lines.append(f"  - {gema.name}: {gema.success_rate:.0%} success rate ({gema.execution_count} executions)")

        # Available models
        available_models = [m for m in self.capability_map.models.values() if m.available]
        if available_models:
            lines.append(f"\nAvailable models: {', '.join(m.name for m in available_models)}")

        # Knowledge boundaries
        high_severity = [b for b in self.knowledge_boundaries if b.severity in ("high", "critical")]
        if high_severity:
            lines.append(f"\nKnown limitations: {len(high_severity)} high-severity boundaries")

        return "\n".join(lines)

    def _invalidate_cache(self):
        """Invalidate identity and description caches"""
        self._identity_cache = None
        self._self_description_cache = ""
        self._cache_timestamp = ""

    def get_identity(self) -> Dict:
        """Get current identity (cached or regenerated)"""
        if not self._identity_cache:
            self._generate_identity()
        return self._identity_cache or {}

    def get_self_description(self) -> str:
        """Get current self-description for system prompt"""
        if not self._self_description_cache:
            self._generate_identity()
        return self._self_description_cache

    def get_best_gema_for_task(self, task: str) -> Optional[str]:
        """
        Get best gema for a task using learned routing rules + discovery.
        
        Priority:
        1. Learned routing rule (if confidence > threshold)
        2. Discovery-based semantic match
        3. Fallback to director
        """
        task_type = self._classify_task_type(task)

        # 1. Check learned routing rules
        with self._routing_lock:
            rule = self.routing_rules.get(task_type)
            if rule and rule.confidence >= 0.7:
                # Verify gema is still available
                if self.capability_map and rule.recommended_gema in self.capability_map.gemas:
                    return rule.recommended_gema

        # 2. Use discovery-based matching
        if self.capability_map:
            gema = self.discovery.get_gema_for_task(task)
            if gema:
                return gema

        # 3. Fallback
        return "director"

    def get_available_models_for_task(self, task_type: str) -> List[str]:
        """Get available models suitable for a task type"""
        capability_map = {
            "coding": "code",
            "debugging": "reasoning",
            "research": "research",
            "testing": "code",
            "design": "reasoning",
            "devops": "code",
            "security": "reasoning",
            "optimization": "code",
            "creative": "creative",
            "analysis": "reasoning",
            "general": "fast",
        }

        required_capability = capability_map.get(task_type, "fast")
        return self.discovery.get_available_models_for_capability(required_capability)

    def record_outcome(
        self,
        task: str,
        gema_used: str,
        success: bool,
        quality: float = 0.0,
        latency_ms: float = 0.0,
    ):
        """Record execution outcome for learning"""
        task_type = self._classify_task_type(task)

        # Update routing rules
        with self._routing_lock:
            if task_type not in self.routing_rules:
                self.routing_rules[task_type] = RoutingRule(
                    task_pattern=task_type,
                    recommended_gema=gema_used,
                    created_at=datetime.now().isoformat(),
                )

            rule = self.routing_rules[task_type]
            if success:
                rule.success_count += 1
            else:
                rule.failure_count += 1

            # Update confidence
            total = rule.success_count + rule.failure_count
            rule.confidence = rule.success_count / total if total > 0 else 0.0
            rule.last_used = datetime.now().isoformat()

            # Update recommended gema if current one performed better
            if success and quality > 0.8:
                rule.recommended_gema = gema_used

        logger.debug(
            f"Outcome recorded: task_type={task_type}, gema={gema_used}, "
            f"success={success}, quality={quality:.2f}"
        )

    def get_knowledge_boundaries(self, severity_filter: str = None) -> List[KnowledgeBoundary]:
        """Get knowledge boundaries, optionally filtered by severity"""
        with self._boundary_lock:
            if severity_filter:
                return [b for b in self.knowledge_boundaries if b.severity == severity_filter]
            return list(self.knowledge_boundaries)

    def get_performance_profile(self, capability_name: str) -> Optional[PerformanceProfile]:
        """Get performance profile for a capability"""
        with self._perf_lock:
            return self.performance_profiles.get(capability_name)

    def get_routing_rules(self) -> Dict[str, RoutingRule]:
        """Get all learned routing rules"""
        with self._routing_lock:
            return dict(self.routing_rules)

    def _load_state(self):
        """Load persisted state from disk"""
        if not self.storage_path or not self.storage_path.exists():
            return

        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))

            # Load routing rules
            for task_type, rule_data in data.get("routing_rules", {}).items():
                self.routing_rules[task_type] = RoutingRule(
                    task_pattern=rule_data.get("task_pattern", task_type),
                    recommended_gema=rule_data.get("recommended_gema", "director"),
                    confidence=rule_data.get("confidence", 0.0),
                    success_count=rule_data.get("success_count", 0),
                    failure_count=rule_data.get("failure_count", 0),
                    created_at=rule_data.get("created_at", ""),
                    last_used=rule_data.get("last_used", ""),
                )

            # Load knowledge boundaries
            for boundary_data in data.get("knowledge_boundaries", []):
                self.knowledge_boundaries.append(KnowledgeBoundary(
                    boundary_type=boundary_data.get("boundary_type", "unknown"),
                    description=boundary_data.get("description", ""),
                    affected_tasks=boundary_data.get("affected_tasks", []),
                    severity=boundary_data.get("severity", "medium"),
                    discovered_at=boundary_data.get("discovered_at", ""),
                    workaround=boundary_data.get("workaround", ""),
                ))

            logger.info(
                f"Self-model state loaded: "
                f"{len(self.routing_rules)} routing rules, "
                f"{len(self.knowledge_boundaries)} boundaries"
            )
        except Exception as e:
            logger.error(f"Failed to load self-model state: {e}")

    def _save_state(self):
        """Persist state to disk"""
        if not self.storage_path:
            return

        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "routing_rules": {
                    task_type: {
                        "task_pattern": rule.task_pattern,
                        "recommended_gema": rule.recommended_gema,
                        "confidence": rule.confidence,
                        "success_count": rule.success_count,
                        "failure_count": rule.failure_count,
                        "created_at": rule.created_at,
                        "last_used": rule.last_used,
                    }
                    for task_type, rule in self.routing_rules.items()
                },
                "knowledge_boundaries": [
                    {
                        "boundary_type": b.boundary_type,
                        "description": b.description,
                        "affected_tasks": b.affected_tasks,
                        "severity": b.severity,
                        "discovered_at": b.discovered_at,
                        "workaround": b.workaround,
                    }
                    for b in self.knowledge_boundaries
                ],
                "saved_at": datetime.now().isoformat(),
            }

            atomic_write_json(self.storage_path, data)

            logger.info("Self-model state saved")
        except Exception as e:
            logger.error(f"Failed to save self-model state: {e}")

    def save_state(self):
        """Public method to save state"""
        self._save_state()

    def get_status(self) -> Dict:
        """Get self-model status summary"""
        return {
            "capability_map_available": self.capability_map is not None,
            "gema_count": len(self.capability_map.gemas) if self.capability_map else 0,
            "performance_profiles": len(self.performance_profiles),
            "knowledge_boundaries": len(self.knowledge_boundaries),
            "routing_rules": len(self.routing_rules),
            "identity_cached": self._identity_cache is not None,
            "cache_timestamp": self._cache_timestamp,
        }
