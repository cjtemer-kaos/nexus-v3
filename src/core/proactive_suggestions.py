"""
ProactiveSuggestions - Sistema de sugerencias proactivas para SuperNEXUS v2.0

Características:
- Análisis de patrones de uso
- Detección automática de oportunidades de mejora
- Sugerencias contextuales basadas en estado del sistema
- Aprendizaje de sugerencias aceptadas/rechazadas
"""

import logging
import hashlib
from typing import Dict, List
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
from enum import Enum

logger = logging.getLogger(__name__)


class SuggestionType(Enum):
    OPTIMIZATION = "optimization"
    SECURITY = "security"
    LEARNING = "learning"
    AUTOMATION = "automation"
    RESOURCE = "resource"
    ARCHITECTURE = "architecture"


class SuggestionPriority(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Suggestion:
    """Sugerencia individual"""
    id: str
    title: str
    description: str
    suggestion_type: SuggestionType
    priority: SuggestionPriority
    created_at: str = ""
    accepted: bool = False
    rejected: bool = False
    implemented: bool = False
    context: Dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.id:
            self.id = hashlib.md5(f"{self.title}{self.created_at}".encode()).hexdigest()[:12]


class ProactiveSuggestions:
    """
    Sistema de sugerencias proactivas para SuperNEXUS v2.0
    
    Analiza el estado del sistema y genera sugerencias automáticas.
    """
    
    def __init__(self):
        self.suggestions: List[Suggestion] = []
        self.patterns: Dict[str, int] = defaultdict(int)
        self.acceptance_history: List[Dict] = []
        self._rules = self._initialize_rules()
    
    def _initialize_rules(self) -> List[Dict]:
        """Inicializa reglas de sugerencias"""
        return [
            {
                "name": "high_cpu_usage",
                "condition": lambda metrics: metrics.get("cpu_percent", 0) > 80,
                "suggestion": Suggestion(
                    id="",
                    title="CPU usage is high",
                    description="Consider offloading tasks to PC2 or reducing parallel execution.",
                    suggestion_type=SuggestionType.RESOURCE,
                    priority=SuggestionPriority.HIGH,
                ),
            },
            {
                "name": "high_ram_usage",
                "condition": lambda metrics: metrics.get("ram_percent", 0) > 85,
                "suggestion": Suggestion(
                    id="",
                    title="RAM usage is high",
                    description="Close unused applications or increase swap space.",
                    suggestion_type=SuggestionType.RESOURCE,
                    priority=SuggestionPriority.HIGH,
                ),
            },
            {
                "name": "offline_engines",
                "condition": lambda status: any(
                    e.get("status") == "offline"
                    for e in status.get("engines", {}).values()
                ),
                "suggestion": Suggestion(
                    id="",
                    title="Engine offline detected",
                    description="Check connectivity to offline engines. Consider fallback options.",
                    suggestion_type=SuggestionType.OPTIMIZATION,
                    priority=SuggestionPriority.CRITICAL,
                ),
            },
            {
                "name": "low_knowledge_confidence",
                "condition": lambda stats: stats.get("avg_confidence", 1.0) < 0.6,
                "suggestion": Suggestion(
                    id="",
                    title="Knowledge base has low confidence",
                    description="Review and validate knowledge pieces to improve system intelligence.",
                    suggestion_type=SuggestionType.LEARNING,
                    priority=SuggestionPriority.MEDIUM,
                ),
            },
            {
                "name": "many_pending_milestones",
                "condition": lambda stats: stats.get("pending_milestones", 0) > 5,
                "suggestion": Suggestion(
                    id="",
                    title="Many pending milestones",
                    description="Consider prioritizing or rescheduling pending project milestones.",
                    suggestion_type=SuggestionType.AUTOMATION,
                    priority=SuggestionPriority.MEDIUM,
                ),
            },
            {
                "name": "repeated_task_pattern",
                "condition": lambda patterns: any(
                    count > 10 for count in patterns.values()
                ),
                "suggestion": Suggestion(
                    id="",
                    title="Repeated task pattern detected",
                    description="Consider automating frequently performed tasks.",
                    suggestion_type=SuggestionType.AUTOMATION,
                    priority=SuggestionPriority.MEDIUM,
                ),
            },
            {
                "name": "no_security_audit",
                "condition": lambda stats: stats.get("last_security_audit_days", 999) > 7,
                "suggestion": Suggestion(
                    id="",
                    title="Security audit overdue",
                    description="Run security audit to ensure system integrity.",
                    suggestion_type=SuggestionType.SECURITY,
                    priority=SuggestionPriority.HIGH,
                ),
            },
        ]
    
    def analyze_and_suggest(
        self,
        metrics: Dict = None,
        engine_status: Dict = None,
        knowledge_stats: Dict = None,
        project_stats: Dict = None,
        task_patterns: Dict = None,
        security_stats: Dict = None,
    ) -> List[Suggestion]:
        """Analiza estado y genera sugerencias"""
        new_suggestions = []
        
        for rule in self._rules:
            try:
                condition_data = {
                    **(metrics or {}),
                    **(engine_status or {}),
                    **(knowledge_stats or {}),
                    **(project_stats or {}),
                    **(security_stats or {}),
                }
                
                if rule["condition"](condition_data):
                    suggestion = Suggestion(
                        id="",
                        title=rule["suggestion"].title,
                        description=rule["suggestion"].description,
                        suggestion_type=rule["suggestion"].suggestion_type,
                        priority=rule["suggestion"].priority,
                        context=condition_data,
                    )
                    
                    if not self._is_duplicate(suggestion):
                        new_suggestions.append(suggestion)
                        self.suggestions.append(suggestion)
                        logger.info(f"New suggestion: {suggestion.title}")
            except Exception as e:
                logger.error(f"Rule evaluation error: {e}")
        
        return new_suggestions
    
    def _is_duplicate(self, suggestion: Suggestion) -> bool:
        """Verifica si sugerencia es duplicada"""
        for existing in self.suggestions[-20:]:
            if (
                existing.title == suggestion.title and
                not existing.accepted and
                not existing.rejected
            ):
                return True
        return False
    
    def accept_suggestion(self, suggestion_id: str) -> bool:
        """Acepta sugerencia"""
        for suggestion in self.suggestions:
            if suggestion.id == suggestion_id:
                suggestion.accepted = True
                self.acceptance_history.append({
                    "suggestion_id": suggestion_id,
                    "action": "accepted",
                    "timestamp": datetime.now().isoformat(),
                })
                self.patterns[suggestion.suggestion_type.value] += 1
                return True
        return False
    
    def reject_suggestion(self, suggestion_id: str) -> bool:
        """Rechaza sugerencia"""
        for suggestion in self.suggestions:
            if suggestion.id == suggestion_id:
                suggestion.rejected = True
                self.acceptance_history.append({
                    "suggestion_id": suggestion_id,
                    "action": "rejected",
                    "timestamp": datetime.now().isoformat(),
                })
                return True
        return False
    
    def mark_implemented(self, suggestion_id: str) -> bool:
        """Marca sugerencia como implementada"""
        for suggestion in self.suggestions:
            if suggestion.id == suggestion_id:
                suggestion.implemented = True
                return True
        return False
    
    def get_active_suggestions(
        self,
        suggestion_type: SuggestionType = None,
        priority: SuggestionPriority = None,
        limit: int = 20,
    ) -> List[Dict]:
        """Obtiene sugerencias activas"""
        active = [
            s for s in self.suggestions
            if not s.accepted and not s.rejected and not s.implemented
        ]
        
        if suggestion_type:
            active = [s for s in active if s.suggestion_type == suggestion_type]
        
        if priority:
            active = [s for s in active if s.priority == priority]
        
        priority_order = {
            SuggestionPriority.CRITICAL: 0,
            SuggestionPriority.HIGH: 1,
            SuggestionPriority.MEDIUM: 2,
            SuggestionPriority.LOW: 3,
        }
        
        active.sort(key=lambda s: priority_order.get(s.priority, 4))
        
        return [
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "type": s.suggestion_type.value,
                "priority": s.priority.value,
                "created_at": s.created_at,
                "context": s.context,
            }
            for s in active[:limit]
        ]
    
    def get_stats(self) -> Dict:
        """Obtiene estadísticas de sugerencias"""
        total = len(self.suggestions)
        accepted = sum(1 for s in self.suggestions if s.accepted)
        rejected = sum(1 for s in self.suggestions if s.rejected)
        implemented = sum(1 for s in self.suggestions if s.implemented)
        active = total - accepted - rejected - implemented
        
        return {
            "total": total,
            "accepted": accepted,
            "rejected": rejected,
            "implemented": implemented,
            "active": active,
            "acceptance_rate": accepted / (accepted + rejected) if (accepted + rejected) > 0 else 0,
            "patterns": dict(self.patterns),
        }
