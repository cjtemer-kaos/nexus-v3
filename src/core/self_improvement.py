"""
SelfImprovement - Sistema de auto-mejora y documentación automática para SuperNEXUS v2.0

Características:
- Análisis de métricas de evolución del sistema
- Documentación automática de cambios y decisiones
- Detección de patrones de error recurrentes
- Sugerencias de optimización basadas en historial
"""

import logging
import json
import hashlib
from typing import Dict, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class ImprovementRecord:
    """Registro de mejora"""
    id: str
    title: str
    description: str
    category: str
    impact: str
    created_at: str = ""
    implemented: bool = False
    metrics_before: Dict = field(default_factory=dict)
    metrics_after: Dict = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.id:
            self.id = hashlib.md5(f"{self.title}{self.created_at}".encode()).hexdigest()[:12]


@dataclass
class ErrorPattern:
    """Patrón de error recurrente"""
    error_type: str
    count: int
    first_seen: str
    last_seen: str
    affected_components: List[str]
    suggested_fix: str = ""


class SelfImprovement:
    """
    Sistema de auto-mejora y documentación automática.
    """
    
    def __init__(self, storage_path: str = None):
        self.improvements: List[ImprovementRecord] = []
        self.error_patterns: Dict[str, ErrorPattern] = {}
        self.decision_log: List[Dict] = []
        self.metrics_history: Dict[str, List] = defaultdict(list)
        self.storage_path = Path(storage_path) if storage_path else None
        
        if self.storage_path and self.storage_path.exists():
            self.load()
    
    def record_improvement(
        self,
        title: str,
        description: str,
        category: str,
        impact: str,
        metrics_before: Dict = None,
        metrics_after: Dict = None,
    ) -> ImprovementRecord:
        """Registra mejora"""
        improvement = ImprovementRecord(
            id="",
            title=title,
            description=description,
            category=category,
            impact=impact,
            metrics_before=metrics_before or {},
            metrics_after=metrics_after or {},
        )
        
        self.improvements.append(improvement)
        logger.info(f"Improvement recorded: {improvement.title}")
        
        self._save()
        return improvement
    
    def log_decision(
        self,
        decision: str,
        rationale: str,
        alternatives: List[str] = None,
        context: Dict = None,
    ):
        """Registra decisión técnica"""
        record = {
            "id": hashlib.md5(f"{decision}{datetime.now()}".encode()).hexdigest()[:12],
            "decision": decision,
            "rationale": rationale,
            "alternatives": alternatives or [],
            "context": context or {},
            "timestamp": datetime.now().isoformat(),
        }
        
        self.decision_log.append(record)
        logger.info(f"Decision logged: {decision}")
        
        self._save()
    
    def record_error(self, error_type: str, component: str, details: str = ""):
        """Registra error para detectar patrones"""
        now = datetime.now().isoformat()
        
        if error_type in self.error_patterns:
            pattern = self.error_patterns[error_type]
            pattern.count += 1
            pattern.last_seen = now
            if component not in pattern.affected_components:
                pattern.affected_components.append(component)
        else:
            self.error_patterns[error_type] = ErrorPattern(
                error_type=error_type,
                count=1,
                first_seen=now,
                last_seen=now,
                affected_components=[component],
            )
        
        if self.error_patterns[error_type].count >= 5:
            logger.warning(f"Error pattern detected: {error_type} ({self.error_patterns[error_type].count} occurrences)")
    
    def record_metric(self, metric_name: str, value: float):
        """Registra métrica para análisis de tendencias"""
        self.metrics_history[metric_name].append({
            "value": value,
            "timestamp": datetime.now().isoformat(),
        })
        
        if len(self.metrics_history[metric_name]) > 1000:
            self.metrics_history[metric_name] = self.metrics_history[metric_name][-1000:]
    
    def analyze_trends(self, metric_name: str, days: int = 7) -> Dict:
        """Analiza tendencias de métrica"""
        history = self.metrics_history.get(metric_name, [])
        
        if not history:
            return {"trend": "no_data"}
        
        cutoff = datetime.now() - timedelta(days=days)
        recent = [
            h for h in history
            if datetime.fromisoformat(h["timestamp"]) > cutoff
        ]
        
        if not recent:
            return {"trend": "no_recent_data"}
        
        values = [h["value"] for h in recent]
        
        avg = sum(values) / len(values)
        min_val = min(values)
        max_val = max(values)
        
        trend = "stable"
        if len(values) > 1:
            if values[-1] > values[0] * 1.1:
                trend = "increasing"
            elif values[-1] < values[0] * 0.9:
                trend = "decreasing"
        
        return {
            "metric": metric_name,
            "trend": trend,
            "avg": avg,
            "min": min_val,
            "max": max_val,
            "samples": len(recent),
            "period_days": days,
        }
    
    def get_recurring_errors(self, min_count: int = 3) -> List[Dict]:
        """Obtiene errores recurrentes"""
        return [
            {
                "error_type": pattern.error_type,
                "count": pattern.count,
                "first_seen": pattern.first_seen,
                "last_seen": pattern.last_seen,
                "affected_components": pattern.affected_components,
                "suggested_fix": pattern.suggested_fix,
            }
            for pattern in self.error_patterns.values()
            if pattern.count >= min_count
        ]
    
    def generate_documentation(self) -> str:
        """Genera documentación automática del sistema"""
        doc = f"""# SuperNEXUS v2.0 - Documentación Automática

Generada: {datetime.now().isoformat()}

## Resumen del Sistema

- **Mejoras implementadas:** {sum(1 for i in self.improvements if i.implemented)}
- **Mejoras pendientes:** {sum(1 for i in self.improvements if not i.implemented)}
- **Decisiones registradas:** {len(self.decision_log)}
- **Errores recurrentes:** {len(self.get_recurring_errors())}

## Mejoras Recientes

"""
        
        for improvement in self.improvements[-10:]:
            status = "✅" if improvement.implemented else "⬜"
            doc += f"### {status} {improvement.title}\n"
            doc += f"- **Categoría:** {improvement.category}\n"
            doc += f"- **Impacto:** {improvement.impact}\n"
            doc += f"- **Fecha:** {improvement.created_at}\n"
            doc += f"- **Descripción:** {improvement.description}\n\n"
        
        doc += "## Decisiones Técnicas\n\n"
        
        for decision in self.decision_log[-10:]:
            doc += f"### {decision['decision']}\n"
            doc += f"- **Racional:** {decision['rationale']}\n"
            doc += f"- **Fecha:** {decision['timestamp']}\n\n"
        
        doc += "## Errores Recurrentes\n\n"
        
        for error in self.get_recurring_errors():
            doc += f"### ⚠️ {error['error_type']} ({error['count']} ocurrencias)\n"
            doc += f"- **Componentes:** {', '.join(error['affected_components'])}\n"
            doc += f"- **Última vez:** {error['last_seen']}\n\n"
        
        return doc
    
    def get_status(self) -> Dict:
        """Obtiene estado de auto-mejora"""
        return {
            "total_improvements": len(self.improvements),
            "implemented_improvements": sum(1 for i in self.improvements if i.implemented),
            "pending_improvements": sum(1 for i in self.improvements if not i.implemented),
            "decision_log_count": len(self.decision_log),
            "error_patterns": len(self.error_patterns),
            "recurring_errors": len(self.get_recurring_errors()),
            "metrics_tracked": len(self.metrics_history),
        }
    
    def _save(self):
        """Guarda en disco"""
        if not self.storage_path:
            return
        
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            "improvements": [
                {
                    "id": i.id,
                    "title": i.title,
                    "description": i.description,
                    "category": i.category,
                    "impact": i.impact,
                    "created_at": i.created_at,
                    "implemented": i.implemented,
                    "metrics_before": i.metrics_before,
                    "metrics_after": i.metrics_after,
                }
                for i in self.improvements
            ],
            "error_patterns": {
                error_type: {
                    "error_type": pattern.error_type,
                    "count": pattern.count,
                    "first_seen": pattern.first_seen,
                    "last_seen": pattern.last_seen,
                    "affected_components": pattern.affected_components,
                    "suggested_fix": pattern.suggested_fix,
                }
                for error_type, pattern in self.error_patterns.items()
            },
            "decision_log": self.decision_log,
        }
        
        atomic_write_json(self.storage_path, data)
    
    def load(self):
        """Carga desde disco"""
        if not self.storage_path or not self.storage_path.exists():
            return
        
        with open(self.storage_path, "r") as f:
            data = json.load(f)
        
        self.improvements = [
            ImprovementRecord(
                id=i["id"],
                title=i["title"],
                description=i["description"],
                category=i["category"],
                impact=i["impact"],
                created_at=i["created_at"],
                implemented=i.get("implemented", False),
                metrics_before=i.get("metrics_before", {}),
                metrics_after=i.get("metrics_after", {}),
            )
            for i in data.get("improvements", [])
        ]
        
        self.error_patterns = {
            error_type: ErrorPattern(
                error_type=pattern["error_type"],
                count=pattern["count"],
                first_seen=pattern["first_seen"],
                last_seen=pattern["last_seen"],
                affected_components=pattern["affected_components"],
                suggested_fix=pattern.get("suggested_fix", ""),
            )
            for error_type, pattern in data.get("error_patterns", {}).items()
        }
        
        self.decision_log = data.get("decision_log", [])
        
        logger.info(f"Self-improvement loaded: {len(self.improvements)} improvements")
