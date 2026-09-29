"""
F14: Memory Health Dashboard

Per-agent memory status (usable, searchable, needs attention).
"""

import logging
import time
from typing import Dict, List
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-memory-health")


@dataclass
class MemoryStatus:
    agent: str
    status: str  # "healthy", "degraded", "critical"
    size_bytes: int = 0
    age_days: float = 0
    entries: int = 0
    searchable: bool = True
    last_access: str = ""
    recommendations: List[str] = None

    def __post_init__(self):
        if self.recommendations is None:
            self.recommendations = []


class MemoryHealthMonitor:
    """Monitors memory health across all agents"""

    def __init__(self):
        self._brain_path = Path.home() / ".nexus" / "brain"
        self._memory_path = Path.home() / ".nexus" / "memory"

    def check_all(self) -> Dict[str, MemoryStatus]:
        agents = {
            "message_board": self._brain_path / "message_board.db",
            "sessions": self._brain_path / "sessions.db",
            "checkpoints": self._brain_path / "checkpoints.db",
            "cerebro": self._memory_path / "cerebro.db",
            "neural": self._memory_path / "neural.db",
            "rag": self._memory_path / "rag.db",
            "knowledge_graph": self._memory_path / "knowledge.db",
            "vault": Path.home() / ".nexus" / "vault" / "vault.db",
        }

        results = {}
        for agent, db_path in agents.items():
            results[agent] = self._check_memory(agent, db_path)

        return results

    def _check_memory(self, agent: str, db_path: Path) -> MemoryStatus:
        recommendations = []

        if not db_path.exists():
            return MemoryStatus(
                agent=agent,
                status="healthy",
                size_bytes=0,
                searchable=True,
                recommendations=["Base de datos no creada aun (normal en primera ejecucion)"],
            )

        size = db_path.stat().st_size
        age = (time.time() - db_path.stat().st_mtime) / 86400

        # Test searchability
        searchable = self._test_searchable(db_path)

        # Determine status
        status = "healthy"
        if size > 100 * 1024 * 1024:  # > 100MB
            status = "degraded"
            recommendations.append(f"Base de datos grande ({size / 1024 / 1024:.1f} MB). Considerar limpieza")
        if size > 500 * 1024 * 1024:  # > 500MB
            status = "critical"
            recommendations.append("Base de datos muy grande. Limpieza urgente requerida")
        if not searchable:
            status = "critical"
            recommendations.append("Base de datos no responde. Puede estar corrupta")
        if age > 30:
            recommendations.append(f"Sin acceso hace {age:.0f} dias. Verificar que el agente funciona")

        # Count entries
        entries = self._count_entries(db_path)

        return MemoryStatus(
            agent=agent,
            status=status,
            size_bytes=size,
            age_days=age,
            entries=entries,
            searchable=searchable,
            last_access=datetime.fromtimestamp(db_path.stat().st_mtime).isoformat(),
            recommendations=recommendations,
        )

    def _test_searchable(self, db_path: Path) -> bool:
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path), timeout=5)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
            c.fetchone()
            conn.close()
            return True
        except Exception:
            return False

    ALLOWED_TABLES = {
        "messages", "shared_memory", "sessions", "notes", "notes_fts",
        "checkpoints", "run_status", "patterns", "knowledge",
    }

    def _count_entries(self, db_path: Path) -> int:
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path), timeout=5)
            c = conn.cursor()
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in c.fetchall()]
            total = 0
            for table in tables:
                if table not in self.ALLOWED_TABLES and not table.startswith("sqlite_"):
                    continue
                try:
                    c.execute(f"SELECT COUNT(*) FROM [{table}]")
                    total += c.fetchone()[0]
                except Exception:
                    pass
            conn.close()
            return total
        except Exception:
            return 0

    def get_summary(self) -> Dict:
        statuses = self.check_all()
        healthy = sum(1 for s in statuses.values() if s.status == "healthy")
        degraded = sum(1 for s in statuses.values() if s.status == "degraded")
        critical = sum(1 for s in statuses.values() if s.status == "critical")
        total_size = sum(s.size_bytes for s in statuses.values())
        total_entries = sum(s.entries for s in statuses.values())

        return {
            "overall": "healthy" if critical == 0 else ("degraded" if degraded > 0 else "critical"),
            "total_memories": len(statuses),
            "healthy": healthy,
            "degraded": degraded,
            "critical": critical,
            "total_size_mb": round(total_size / 1024 / 1024, 1),
            "total_entries": total_entries,
            "details": {
                agent: {
                    "status": s.status,
                    "size_mb": round(s.size_bytes / 1024 / 1024, 1),
                    "entries": s.entries,
                    "searchable": s.searchable,
                    "recommendations": s.recommendations,
                }
                for agent, s in statuses.items()
            },
        }

    def get_report(self) -> Dict:
        return self.get_summary()
