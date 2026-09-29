"""
SelfImprovingLoop — Autoresearch-inspired self-improvement loop for SuperNEXUS v2.

Inspired by Karpathy's autoresearch pattern: the system logs experiments
(hypothesis → approach → result), tracks which ones succeed, and
automatically suggests new improvements based on observed patterns.

Storage: SQLite at ~/.nexus/brain/self_improving.db

Tables:
  experiments — id, hypothesis, approach, result, success, metrics (JSON),
                created_at, completed_at
  improvements — id, area, description, impact, implemented, verified,
                 created_at

Design:
  Singleton via get_self_improving_loop() — one instance per process.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_self_improving_singleton: SelfImprovingLoop | None = None


def get_self_improving_loop() -> SelfImprovingLoop:
    """Get or create the singleton SelfImprovingLoop instance."""
    global _self_improving_singleton
    if _self_improving_singleton is None:
        _self_improving_singleton = SelfImprovingLoop()
    return _self_improving_singleton


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class SelfImprovingLoop:
    """
    Self-improving experiment tracking system.

    Keeps a persistent SQLite log of experiments and improvements.
    The loop works as follows:

        1. log_experiment(hypothesis, approach) → experiment_id
        2. (run the experiment externally)
        3. complete_experiment(id, result, success, metrics)
        4. After enough experiments, analyze_patterns() detects
           successful approaches and suggests new improvements.
        5. Daily digest() summarises progress.
    """

    DB_PATH = Path.home() / ".nexus" / "brain" / "self_improving.db"

    def __init__(self, db_path: str | Path | None = None):
        self._db_path = Path(db_path) if db_path else self.DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    hypothesis      TEXT    NOT NULL,
                    approach        TEXT    NOT NULL,
                    result          TEXT    DEFAULT '',
                    success         INTEGER DEFAULT 0,   -- 0/1 bool
                    metrics         TEXT    DEFAULT '{}', -- JSON dict
                    created_at      TEXT    NOT NULL,
                    completed_at    TEXT
                );

                CREATE TABLE IF NOT EXISTS improvements (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    area            TEXT    NOT NULL,
                    description     TEXT    NOT NULL,
                    impact          TEXT    NOT NULL,
                    implemented     INTEGER DEFAULT 0,   -- 0/1 bool
                    verified        INTEGER DEFAULT 0,   -- 0/1 bool
                    created_at      TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_exp_success ON experiments(success);
                CREATE INDEX IF NOT EXISTS idx_imp_pending ON improvements(implemented, verified);
            """)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        """Open a new connection to the DB."""
        return sqlite3.connect(str(self._db_path), timeout=10)

    # ------------------------------------------------------------------
    # Experiments
    # ------------------------------------------------------------------

    def log_experiment(self, hypothesis: str, approach: str) -> int:
        """
        Log the start of a new experiment.

        Args:
            hypothesis: What we expect to happen.
            approach: How we plan to test it.

        Returns:
            experiment_id (int)
        """
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO experiments (hypothesis, approach, created_at) VALUES (?, ?, ?)",
                (hypothesis, approach, now),
            )
            conn.commit()
            eid = cur.lastrowid
            logger.info("Experiment %d logged: %s", eid, hypothesis[:80])
            return eid
        finally:
            conn.close()

    def complete_experiment(
        self,
        experiment_id: int,
        result: str,
        success: bool,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        """
        Mark an experiment as completed.

        Args:
            experiment_id: ID returned by log_experiment().
            result: Summary of what happened.
            success: Whether the hypothesis was confirmed.
            metrics: Optional numeric metrics (latency, accuracy, etc.).
        """
        now = datetime.now().isoformat()
        metrics_json = json.dumps(metrics or {}, ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute(
                """UPDATE experiments
                   SET result = ?, success = ?, metrics = ?, completed_at = ?
                   WHERE id = ?""",
                (result, int(success), metrics_json, now, experiment_id),
            )
            conn.commit()
            status = "SUCCESS" if success else "FAILURE"
            logger.info("Experiment %d completed [%s]: %s", experiment_id, status, result[:80])
        finally:
            conn.close()

    def get_successful_experiments(self, limit: int = 10) -> list[dict]:
        """Return the most recent successful experiments."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, hypothesis, approach, result, metrics, created_at, completed_at
                   FROM experiments
                   WHERE success = 1
                   ORDER BY completed_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "id": r[0],
                "hypothesis": r[1],
                "approach": r[2],
                "result": r[3],
                "metrics": json.loads(r[4]) if r[4] else {},
                "created_at": r[5],
                "completed_at": r[6],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Improvements
    # ------------------------------------------------------------------

    def suggest_improvement(self, area: str, description: str, impact: str) -> int:
        """
        Suggest a new improvement.

        Args:
            area: System area (e.g. "retrieval", "routing", "memory").
            description: What to change and why.
            impact: Expected impact (e.g. "high", "medium", "low").

        Returns:
            improvement_id (int)
        """
        now = datetime.now().isoformat()
        conn = self._connect()
        try:
            cur = conn.execute(
                "INSERT INTO improvements (area, description, impact, created_at) VALUES (?, ?, ?, ?)",
                (area, description, impact, now),
            )
            conn.commit()
            iid = cur.lastrowid
            logger.info("Improvement %d suggested: [%s] %s", iid, area, description[:80])
            return iid
        finally:
            conn.close()

    def implement_improvement(self, improvement_id: int) -> None:
        """Mark an improvement as implemented."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE improvements SET implemented = 1 WHERE id = ?",
                (improvement_id,),
            )
            conn.commit()
            logger.info("Improvement %d marked as implemented", improvement_id)
        finally:
            conn.close()

    def verify_improvement(self, improvement_id: int) -> None:
        """Mark an improvement as verified (worked as expected)."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE improvements SET verified = 1 WHERE id = ?",
                (improvement_id,),
            )
            conn.commit()
            logger.info("Improvement %d marked as verified", improvement_id)
        finally:
            conn.close()

    def get_pending_improvements(self) -> list[dict]:
        """Return improvements that haven't been implemented yet."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT id, area, description, impact, created_at
                   FROM improvements
                   WHERE implemented = 0
                   ORDER BY
                     CASE impact
                       WHEN 'high' THEN 1
                       WHEN 'medium' THEN 2
                       WHEN 'low' THEN 3
                       ELSE 4
                     END,
                     created_at DESC"""
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "id": r[0],
                "area": r[1],
                "description": r[2],
                "impact": r[3],
                "created_at": r[4],
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Pattern analysis
    # ------------------------------------------------------------------

    def analyze_patterns(self) -> dict:
        """
        Analyse successful experiments to discover patterns
        and automatically suggest new improvements.

        Returns a dict with:
          - patterns_found: list of detected patterns
          - suggestions: list of improvement suggestions created
          - summary: human-readable summary
        """
        conn = self._connect()
        try:
            # Gather all successful experiments
            rows = conn.execute(
                """SELECT id, hypothesis, approach, result, metrics
                   FROM experiments
                   WHERE success = 1
                   ORDER BY completed_at DESC
                   LIMIT 50"""
            ).fetchall()
        finally:
            conn.close()

        if not rows:
            return {
                "patterns_found": [],
                "suggestions": [],
                "summary": "No successful experiments yet — nothing to analyse.",
            }

        # --- Detect patterns ---
        patterns: list[dict] = []
        area_frequency: dict[str, int] = {}
        approach_keywords: dict[str, int] = {}

        for row in rows:
            eid, hypothesis, approach, result, metrics_json = row
            hypothesis_lower = (hypothesis or "").lower()
            approach_lower = (approach or "").lower()

            # Extract area keywords from hypothesis/approach
            for kw in ["retrieval", "memory", "routing", "prompt", "model",
                        "cache", "rag", "embedding", "agent", "tool",
                        "optimization", "compression", "consolidation"]:
                if kw in hypothesis_lower or kw in approach_lower:
                    area_frequency[kw] = area_frequency.get(kw, 0) + 1

            # Tokenise approach words (length > 3, skip stopwords)
            stopwords = {"the", "and", "with", "that", "this", "from", "using",
                         "para", "como", "desde", "hacia", "sobre", "entre"}
            for word in approach_lower.split():
                if len(word) > 3 and word.isalpha() and word not in stopwords:
                    approach_keywords[word] = approach_keywords.get(word, 0) + 1

        # Top areas where experiments succeed
        sorted_areas = sorted(area_frequency.items(), key=lambda x: x[1], reverse=True)
        if sorted_areas:
            patterns.append({
                "type": "successful_areas",
                "description": "Areas with highest experiment success",
                "data": sorted_areas[:5],
            })

        # Top approach keywords
        sorted_approaches = sorted(approach_keywords.items(), key=lambda x: x[1], reverse=True)
        if sorted_approaches:
            patterns.append({
                "type": "common_approach_terms",
                "description": "Most frequent approach keywords in successful experiments",
                "data": sorted_approaches[:10],
            })

        # --- Auto-suggest improvements ---
        suggestions: list[dict] = []

        if sorted_areas:
            top_area, top_count = sorted_areas[0]
            if top_count >= 3:
                desc = (
                    f"Systematically document and formalise the '{top_area}' approach "
                    f"({top_count} successful experiments used it). Create a reusable "
                    f"playbook or skill for this area."
                )
                iid = self.suggest_improvement(
                    area=top_area,
                    description=desc,
                    impact="high",
                )
                suggestions.append({"id": iid, "area": top_area, "description": desc})

        if sorted_approaches and len(sorted_approaches) >= 2:
            top_term = sorted_approaches[0][0]
            desc = (
                f"Investigate deeper integration of '{top_term}' pattern "
                f"(appeared in {sorted_approaches[0][1]} successful experiments). "
                f"Consider promoting to a first-class strategy."
            )
            iid = self.suggest_improvement(
                area="strategy",
                description=desc,
                impact="medium",
            )
            suggestions.append({"id": iid, "area": "strategy", "description": desc})

        # Suggest cross-area integration if >= 2 areas are active
        if len(sorted_areas) >= 2:
            a1, a2 = sorted_areas[0][0], sorted_areas[1][0]
            desc = (
                f"Explore integration between '{a1}' and '{a2}' — "
                f"both have high success rates. A combined approach could "
                f"yield compound improvements."
            )
            iid = self.suggest_improvement(
                area="cross-area",
                description=desc,
                impact="medium",
            )
            suggestions.append({"id": iid, "area": "cross-area", "description": desc})

        summary = (
            f"Analysed {len(rows)} successful experiments. "
            f"Detected {len(patterns)} patterns. "
            f"Generated {len(suggestions)} improvement suggestions."
        )
        logger.info("Pattern analysis: %s", summary)

        return {
            "patterns_found": patterns,
            "suggestions": suggestions,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return overall statistics for the self-improving loop."""
        conn = self._connect()
        try:
            total_exp = conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]
            successful_exp = conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE success = 1"
            ).fetchone()[0]
            failed_exp = conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE success = 0 AND completed_at IS NOT NULL"
            ).fetchone()[0]
            pending_exp = conn.execute(
                "SELECT COUNT(*) FROM experiments WHERE completed_at IS NULL"
            ).fetchone()[0]

            total_imp = conn.execute("SELECT COUNT(*) FROM improvements").fetchone()[0]
            implemented_imp = conn.execute(
                "SELECT COUNT(*) FROM improvements WHERE implemented = 1"
            ).fetchone()[0]
            verified_imp = conn.execute(
                "SELECT COUNT(*) FROM improvements WHERE verified = 1"
            ).fetchone()[0]
            pending_imp = conn.execute(
                "SELECT COUNT(*) FROM improvements WHERE implemented = 0"
            ).fetchone()[0]

            # Last experiment timestamp
            last_exp_row = conn.execute(
                "SELECT completed_at FROM experiments WHERE completed_at IS NOT NULL "
                "ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            last_experiment = last_exp_row[0] if last_exp_row else None
        finally:
            conn.close()

        return {
            "experiments": {
                "total": total_exp,
                "successful": successful_exp,
                "failed": failed_exp,
                "pending": pending_exp,
                "success_rate": (
                    round(successful_exp / (successful_exp + failed_exp) * 100, 1)
                    if (successful_exp + failed_exp) > 0 else 0.0
                ),
            },
            "improvements": {
                "total": total_imp,
                "implemented": implemented_imp,
                "verified": verified_imp,
                "pending": pending_imp,
            },
            "last_experiment": last_experiment,
            "db_path": str(self._db_path),
        }

    # ------------------------------------------------------------------
    # Daily digest
    # ------------------------------------------------------------------

    def digest(self) -> str:
        """
        Generate a daily digest summarising recent activity.

        Covers the last 24 hours of experiments and improvements.
        """
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
        conn = self._connect()
        try:
            recent_exp = conn.execute(
                """SELECT id, hypothesis, approach, result, success, created_at
                   FROM experiments
                   WHERE created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,),
            ).fetchall()

            recent_imp = conn.execute(
                """SELECT id, area, description, impact, implemented, created_at
                   FROM improvements
                   WHERE created_at >= ?
                   ORDER BY created_at DESC""",
                (cutoff,),
            ).fetchall()

            stats = self.get_stats()
        finally:
            conn.close()

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"# 🧠 Self-Improving Loop — Daily Digest",
            f"*Generated: {now_str}*",
            "",
            "## Overall Stats",
            f"- Experiments: {stats['experiments']['total']} total "
            f"({stats['experiments']['successful']} ✅ / {stats['experiments']['failed']} ❌ / "
            f"{stats['experiments']['pending']} ⏳) — "
            f"success rate: {stats['experiments']['success_rate']}%",
            f"- Improvements: {stats['improvements']['total']} total "
            f"({stats['improvements']['implemented']} implemented / "
            f"{stats['improvements']['verified']} verified / "
            f"{stats['improvements']['pending']} pending)",
            "",
        ]

        # Last 24h experiments
        lines.append("## Experiments (last 24h)")
        if recent_exp:
            for row in recent_exp:
                eid, hyp, appr, res, success, cat = row
                icon = "✅" if success else ("⏳" if not res else "❌")
                lines.append(f"- {icon} **#{eid}** — {hyp[:60]}")
                if res:
                    lines.append(f"  - Result: {res[:100]}")
        else:
            lines.append("- No experiments in the last 24h.")

        lines.append("")

        # Last 24h improvements
        lines.append("## Improvements (last 24h)")
        if recent_imp:
            for row in recent_imp:
                iid, area, desc, impact, impl, cat = row
                icon = "✅" if impl else "💡"
                lines.append(f"- {icon} **#{iid}** [{area}] ({impact}) — {desc[:80]}")
        else:
            lines.append("- No improvements suggested in the last 24h.")

        lines.append("")

        # Pattern analysis
        lines.append("## Pattern Analysis")
        analysis = self.analyze_patterns()
        lines.append(f"- {analysis['summary']}")
        for pattern in analysis["patterns_found"]:
            lines.append(f"  - **{pattern['type']}**: {pattern['description']}")
            if pattern.get("data"):
                for item in pattern["data"][:3]:
                    if isinstance(item, tuple):
                        lines.append(f"    - `{item[0]}` × {item[1]}")
                    else:
                        lines.append(f"    - {item}")

        digest_text = "\n".join(lines)
        logger.info("Daily digest generated (%d lines)", len(lines))
        return digest_text
