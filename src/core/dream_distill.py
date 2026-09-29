"""
Dream/Distill — Self-improvement cycles.

Dream:  consolidacion semanal (7d) de observaciones → insights de alto nivel
Distill: descubrimiento mensual (30d) de patrones recurrentes en insights

Cada ciclo genera:
  - insight sintetico
  - recomendaciones accionables
  - patrones detectados
  - metricas de confianza
"""

import json
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-dream")


class DreamType(str, Enum):
    DREAM = "dream"
    DISTILL = "distill"


@dataclass
class DreamInsight:
    id: str = ""
    dream_type: str = "dream"
    title: str = ""
    summary: str = ""
    patterns: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    confidence: float = 0.0
    source_ids: List[str] = field(default_factory=list)
    topics: List[str] = field(default_factory=list)
    created_at: str = ""
    cycle: int = 0

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:12]
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


@dataclass
class DreamCycle:
    id: str = ""
    dream_type: str = "dream"
    cycle: int = 0
    status: str = "pending"
    started_at: str = ""
    completed_at: str = ""
    insight_count: int = 0
    summary: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())[:8]
        if not self.started_at:
            self.started_at = datetime.now().isoformat()


class DreamDistillEngine:

    def __init__(self, director=None, db_path: Optional[str] = None, get_observations_fn=None):
        self.director = director
        self.get_observations_fn = get_observations_fn
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "dream.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._insights: Dict[str, DreamInsight] = {}
        self._cycles: Dict[str, DreamCycle] = {}

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS dream_insights (
            id TEXT PRIMARY KEY,
            dream_type TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT DEFAULT '',
            patterns_json TEXT DEFAULT '[]',
            recommendations_json TEXT DEFAULT '[]',
            confidence REAL DEFAULT 0.0,
            source_ids_json TEXT DEFAULT '[]',
            topics_json TEXT DEFAULT '[]',
            created_at TEXT NOT NULL,
            cycle INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS dream_cycles (
            id TEXT PRIMARY KEY,
            dream_type TEXT NOT NULL,
            cycle INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            started_at TEXT NOT NULL,
            completed_at TEXT DEFAULT '',
            insight_count INTEGER DEFAULT 0,
            summary TEXT DEFAULT '',
            metadata_json TEXT DEFAULT '{}'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS dream_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL,
            dream_type TEXT DEFAULT '',
            cycle INTEGER DEFAULT 0,
            details TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )""")
        conn.commit()
        conn.close()

    def _save_insight(self, insight: DreamInsight):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO dream_insights
            (id, dream_type, title, summary, patterns_json, recommendations_json,
             confidence, source_ids_json, topics_json, created_at, cycle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            insight.id, insight.dream_type, insight.title, insight.summary,
            json.dumps(insight.patterns, ensure_ascii=False),
            json.dumps(insight.recommendations, ensure_ascii=False),
            insight.confidence,
            json.dumps(insight.source_ids, ensure_ascii=False),
            json.dumps(insight.topics, ensure_ascii=False),
            insight.created_at, insight.cycle,
        ))
        conn.commit()
        conn.close()

    def _save_cycle(self, cycle: DreamCycle):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""INSERT OR REPLACE INTO dream_cycles
            (id, dream_type, cycle, status, started_at, completed_at,
             insight_count, summary, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
            cycle.id, cycle.dream_type, cycle.cycle, cycle.status,
            cycle.started_at, cycle.completed_at, cycle.insight_count,
            cycle.summary, json.dumps(cycle.metadata, ensure_ascii=False),
        ))
        conn.commit()
        conn.close()

    def _log(self, event: str, dream_type: str = "", cycle: int = 0, details: str = ""):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("INSERT INTO dream_log (event, dream_type, cycle, details, created_at) VALUES (?, ?, ?, ?, ?)",
                  (event, dream_type, cycle, details, datetime.now().isoformat()))
        conn.commit()
        conn.close()

    def _get_next_cycle(self, dream_type: str) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COALESCE(MAX(cycle), 0) FROM dream_cycles WHERE dream_type = ?", (dream_type,))
        max_cycle = c.fetchone()[0]
        conn.close()
        return max_cycle + 1

    def _get_observations(self) -> List[Dict]:
        if self.get_observations_fn:
            return self.get_observations_fn()
        return []

    async def dream(self) -> DreamCycle:
        dream_type = "dream"
        cycle_num = self._get_next_cycle(dream_type)
        cycle = DreamCycle(dream_type=dream_type, cycle=cycle_num, status="running")
        self._cycles[cycle.id] = cycle
        self._save_cycle(cycle)
        self._log("dream_start", dream_type, cycle_num, f"Cycle {cycle_num} started")
        t0 = time.time()

        try:
            observations = self._get_observations()
            if not observations:
                observations = self._load_recent_compose_runs()

            insights = await self._generate_insights(dream_type, observations, cycle_num)
            for ins in insights:
                self._save_insight(ins)
                self._insights[ins.id] = ins

            summary = self._build_summary(dream_type, insights)
            cycle.status = "completed"
            cycle.completed_at = datetime.now().isoformat()
            cycle.insight_count = len(insights)
            cycle.summary = summary
            cycle.metadata = {
                "duration_s": round(time.time() - t0, 2),
                "observation_count": len(observations),
                "insight_count": len(insights),
            }
            self._save_cycle(cycle)
            self._log("dream_complete", dream_type, cycle_num,
                       f"{len(insights)} insights in {cycle.metadata['duration_s']:.1f}s")
        except Exception as e:
            logger.exception(f"Dream cycle {cycle_num} failed: {e}")
            cycle.status = "failed"
            cycle.completed_at = datetime.now().isoformat()
            cycle.metadata["error"] = str(e)
            self._save_cycle(cycle)
            self._log("dream_fail", dream_type, cycle_num, str(e))

        return cycle

    async def distill(self) -> DreamCycle:
        dream_type = "distill"
        cycle_num = self._get_next_cycle(dream_type)
        cycle = DreamCycle(dream_type=dream_type, cycle=cycle_num, status="running")
        self._cycles[cycle.id] = cycle
        self._save_cycle(cycle)
        self._log("distill_start", dream_type, cycle_num, f"Cycle {cycle_num} started")
        t0 = time.time()

        try:
            dream_insights = self._load_insights("dream")
            compose_runs = self._load_recent_compose_runs(days=30)

            pattern_prompt = (
                "Analiza los siguientes datos y descubre patrones recurrentes:\n\n"
                f"--- Dream Insights ({len(dream_insights)}): ---\n"
                + json.dumps(dream_insights[:10], ensure_ascii=False)[:2000]
                + f"\n--- Compose Runs ({len(compose_runs)}): ---\n"
                + json.dumps(compose_runs[:5], ensure_ascii=False)[:2000]
                + "\n\nIdentifica: patrones de exito, cuellos de botella frecuentes, "
                "areas de mejora, tecnologias trending, antipatrones."
            )

            if self.director:
                result = await self._llm_call(pattern_prompt)
                raw = result.get("response", result.get("result", ""))
            else:
                raw = f"[mock-distill] Patrones detectados en {len(dream_insights)} insights y {len(compose_runs)} runs"

            insight = DreamInsight(
                dream_type="distill",
                title=f"Distill Cycle {cycle_num}",
                summary=raw[:500],
                patterns=self._extract_patterns(raw),
                recommendations=self._extract_recommendations(raw),
                confidence=0.7,
                cycle=cycle_num,
                topics=["pattern-discovery", "cross-cycle-analysis"],
            )
            self._save_insight(insight)
            self._insights[insight.id] = insight

            cycle.status = "completed"
            cycle.completed_at = datetime.now().isoformat()
            cycle.insight_count = 1
            cycle.summary = f"Distill cycle {cycle_num}: {len(insight.patterns)} patrones identificados"
            cycle.metadata = {
                "duration_s": round(time.time() - t0, 2),
                "dream_insights_analyzed": len(dream_insights),
                "compose_runs_analyzed": len(compose_runs),
            }
            self._save_cycle(cycle)
            self._log("distill_complete", dream_type, cycle_num,
                       f"{len(insight.patterns)} patrones en {cycle.metadata['duration_s']:.1f}s")
        except Exception as e:
            logger.exception(f"Distill cycle {cycle_num} failed: {e}")
            cycle.status = "failed"
            cycle.metadata["error"] = str(e)
            self._save_cycle(cycle)
            self._log("distill_fail", dream_type, cycle_num, str(e))

        return cycle

    async def _generate_insights(self, dream_type: str, observations: List[Dict], cycle: int) -> List[DreamInsight]:
        if not observations:
            return [DreamInsight(
                dream_type=dream_type, title="No data available",
                summary="No observations to analyze this cycle",
                confidence=0.0, cycle=cycle,
            )]

        batch_size = 5
        insights = []
        for i in range(0, len(observations), batch_size):
            batch = observations[i:i + batch_size]
            prompt = (
                "Eres un ingeniero de staff analizando datos de sesiones de desarrollo.\n\n"
                f"Datos del ciclo {cycle} (lote {i // batch_size + 1}):\n"
                + json.dumps(batch, ensure_ascii=False)[:2000] +
                "\n\nGenera un insight estructurado:\n"
                "1. Titulo conciso\n"
                "2. Resumen (2-3 frases)\n"
                "3. Patrones observados (lista)\n"
                "4. Recomendaciones accionables (lista)\n"
                "5. Confianza (0.0-1.0)\n"
                "6. Topics relevantes (lista)\n\n"
                "Formato JSON."
            )

            if self.director:
                result = await self._llm_call(prompt)
                raw = result.get("response", result.get("result", ""))
                parsed = self._try_parse_json(raw)
            else:
                parsed = {
                    "title": f"Insight lote {i // batch_size + 1} ciclo {cycle}",
                    "summary": f"Analisis de {len(batch)} observaciones",
                    "patterns": ["patron ejemplo"],
                    "recommendations": ["mejorar logging", "revisar errores frecuentes"],
                    "confidence": 0.6,
                    "topics": ["observaciones", "patrones"],
                }

            if isinstance(parsed, dict):
                ins = DreamInsight(
                    dream_type=dream_type, cycle=cycle,
                    title=parsed.get("title", f"Insight batch {i // batch_size + 1}"),
                    summary=parsed.get("summary", "")[:500],
                    patterns=parsed.get("patterns", []),
                    recommendations=parsed.get("recommendations", []),
                    confidence=float(parsed.get("confidence", 0.5)),
                    source_ids=[o.get("id", "") for o in batch if o.get("id")],
                    topics=parsed.get("topics", []),
                )
                insights.append(ins)

        if not insights:
            insights.append(DreamInsight(
                dream_type=dream_type,
                title=f"Dream cycle {cycle}",
                summary=f"Procesados {len(observations)} observaciones",
                confidence=0.5, cycle=cycle,
            ))

        return insights

    def _build_summary(self, dream_type: str, insights: List[DreamInsight]) -> str:
        if not insights:
            return "No insights generated"
        total_conf = sum(i.confidence for i in insights)
        topics = set()
        for i in insights:
            topics.update(i.topics)
        return (
            f"{dream_type.title()} cycle: {len(insights)} insights, "
            f"confianza promedio {total_conf / len(insights):.2f}, "
            f"topics: {', '.join(list(topics)[:5])}"
        )

    def _extract_patterns(self, raw: str) -> List[str]:
        parsed = self._try_parse_json(raw)
        if isinstance(parsed, dict):
            patterns = parsed.get("patterns", [])
            if isinstance(patterns, list):
                return [str(p) for p in patterns]
        return ["patron recurrente detectado"]

    def _extract_recommendations(self, raw: str) -> List[str]:
        parsed = self._try_parse_json(raw)
        if isinstance(parsed, dict):
            recs = parsed.get("recommendations", [])
            if isinstance(recs, list):
                return [str(r) for r in recs]
        return ["revisar datos para mas recomendaciones"]

    def _load_recent_compose_runs(self, days: int = 7) -> List[Dict]:
        compose_db = Path.home() / ".nexus" / "brain" / "compose.db"
        if not compose_db.exists():
            return []
        try:
            conn = sqlite3.connect(str(compose_db))
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            c.execute("SELECT id, spec, goal, status, summary, created_at FROM compose_runs WHERE created_at >= ? ORDER BY created_at DESC", (cutoff,))
            rows = [dict(r) for r in c.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.warning(f"Cannot read compose db: {e}")
            return []

    def _load_insights(self, dream_type: str) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM dream_insights WHERE dream_type = ? ORDER BY created_at DESC LIMIT 50", (dream_type,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        for r in rows:
            for key in ("patterns_json", "recommendations_json", "source_ids_json", "topics_json"):
                if isinstance(r.get(key), str):
                    try:
                        r[key.replace("_json", "")] = json.loads(r[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
        return rows

    def _load_insight_objects(self, dream_type: str, limit: int = 50) -> List[DreamInsight]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM dream_insights WHERE dream_type = ? ORDER BY created_at DESC LIMIT ?", (dream_type, limit))
        rows = c.fetchall()
        conn.close()
        result = []
        for r in rows:
             result.append(DreamInsight(
                id=r["id"], dream_type=r["dream_type"], title=r["title"],
                summary=r["summary"],
                patterns=json.loads(r["patterns_json"] or "[]"),
                recommendations=json.loads(r["recommendations_json"] or "[]"),
                confidence=r["confidence"],
                source_ids=json.loads(r["source_ids_json"] or "[]"),
                topics=json.loads(r["topics_json"] or "[]"),
                created_at=r["created_at"], cycle=r["cycle"],
            ))
        return result

    def get_cycles(self, dream_type: str = "", limit: int = 20) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if dream_type:
            c.execute("SELECT * FROM dream_cycles WHERE dream_type = ? ORDER BY cycle DESC LIMIT ?", (dream_type, limit))
        else:
            c.execute("SELECT * FROM dream_cycles ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_insights(self, dream_type: str = "", limit: int = 50) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if dream_type:
            c.execute("SELECT * FROM dream_insights WHERE dream_type = ? ORDER BY created_at DESC LIMIT ?", (dream_type, limit))
        else:
            c.execute("SELECT * FROM dream_insights ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_logs(self, limit: int = 50) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM dream_log ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows

    def get_stats(self) -> Dict:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM dream_cycles WHERE dream_type = 'dream'")
        dream_cycles = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM dream_cycles WHERE dream_type = 'distill'")
        distill_cycles = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM dream_insights WHERE dream_type = 'dream'")
        dream_insights = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM dream_insights WHERE dream_type = 'distill'")
        distill_insights = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM dream_cycles WHERE status = 'completed' AND dream_type = 'dream'")
        dream_completed = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM dream_cycles WHERE status = 'completed' AND dream_type = 'distill'")
        distill_completed = c.fetchone()[0]
        conn.close()
        return {
            "dream_cycles": dream_cycles,
            "dream_completed": dream_completed,
            "distill_cycles": distill_cycles,
            "distill_completed": distill_completed,
            "dream_insights": dream_insights,
            "distill_insights": distill_insights,
            "db_path": self.db_path,
        }

    async def _llm_call(self, prompt: str) -> Dict:
        if not self.director:
            return {"response": f"[mock] {prompt[:100]}...", "status": "mock"}
        try:
            gema = self.director.gemas.get("sage")
            if not gema:
                gema = list(self.director.gemas.values())[0]
            result = await self.director.execute(gema_name=gema.name, task=prompt)
            return {"response": result.get("response", result.get("result", str(result)))}
        except Exception as e:
            return {"response": "", "error": str(e)}

    def _try_parse_json(self, text: str) -> Any:
        text = text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            start = next((i for i, line in enumerate(lines) if line.strip().startswith("```")), 0)
            text = "\n".join(lines[start + 1:])
            end = text.rfind("```")
            if end >= 0:
                text = text[:end].strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None
