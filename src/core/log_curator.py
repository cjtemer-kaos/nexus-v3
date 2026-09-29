"""
Log Curator — Deduplicación y filtrado de logs/memorias para mantener calidad.
Usa hash-based dedup adaptado a SQLite local.
"""

import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("NEXUS_DATA", os.path.expanduser("~/.nexus"))) / "curator.db"


def _normalize(text: str) -> str:
    """Normalize text for dedup."""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[^\w\s]', '', text)
    return text


def _content_hash(text: str) -> str:
    """MD5 hash of normalized text."""
    return hashlib.md5(_normalize(text).encode()).hexdigest()


def _ngram_fingerprint(text: str, n: int = 5) -> set:
    """N-gram fingerprint for fuzzy dedup."""
    words = _normalize(text).split()
    if len(words) < n:
        return {_normalize(text)}
    return {" ".join(words[i:i+n]) for i in range(len(words) - n + 1)}


class LogCurator:
    """Curates logs, memories, and conversation history."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_hashes (
                    hash TEXT PRIMARY KEY,
                    first_seen TEXT,
                    count INTEGER DEFAULT 1,
                    source TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS curation_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    total_processed INTEGER,
                    duplicates_removed INTEGER,
                    low_quality_removed INTEGER,
                    source TEXT
                )
            """)

    def is_duplicate(self, text: str, source: str = "unknown") -> bool:
        """Check if content is duplicate (exact match via hash)."""
        h = _content_hash(text)
        with sqlite3.connect(str(self.db_path)) as conn:
            row = conn.execute("SELECT count FROM seen_hashes WHERE hash = ?", (h,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE seen_hashes SET count = count + 1 WHERE hash = ?", (h,)
                )
                return True
            conn.execute(
                "INSERT INTO seen_hashes (hash, first_seen, count, source) VALUES (?, ?, 1, ?)",
                (h, datetime.now().isoformat(), source),
            )
            return False

    def quality_score(self, text: str) -> float:
        """Score text quality 0-1."""
        if not text or not text.strip():
            return 0.0

        score = 1.0
        words = text.split()

        # Too short
        if len(words) < 3:
            score *= 0.3

        # Too repetitive (high ratio of repeated words)
        unique_ratio = len(set(words)) / max(len(words), 1)
        if unique_ratio < 0.3:
            score *= 0.4

        # Mostly numbers/symbols
        alpha_ratio = sum(1 for c in text if c.isalpha()) / max(len(text), 1)
        if alpha_ratio < 0.3:
            score *= 0.5

        # Error/garbage patterns
        garbage_patterns = [
            r'error.*error.*error',
            r'null.*null.*null',
            r'undefined.*undefined',
            r'\[object Object\]',
        ]
        for pattern in garbage_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                score *= 0.3

        return min(score, 1.0)

    def curate_batch(
        self,
        entries: List[Dict],
        content_key: str = "content",
        source: str = "logs",
        min_quality: float = 0.3,
    ) -> Tuple[List[Dict], Dict]:
        """
        Curate a batch of entries:
        1. Dedup via content hash
        2. Quality filter
        3. Return clean entries + stats
        """
        clean = []
        stats = {"total": len(entries), "duplicates": 0, "low_quality": 0, "kept": 0}

        for entry in entries:
            text = entry.get(content_key, "")
            if not text:
                stats["low_quality"] += 1
                continue

            if self.is_duplicate(text, source):
                stats["duplicates"] += 1
                continue

            quality = self.quality_score(text)
            if quality < min_quality:
                stats["low_quality"] += 1
                continue

            entry["_quality_score"] = quality
            clean.append(entry)
            stats["kept"] += 1

        # Record stats
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO curation_stats (timestamp, total_processed, duplicates_removed, low_quality_removed, source) VALUES (?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), stats["total"], stats["duplicates"], stats["low_quality"], source),
            )

        logger.info(
            f"Curated {source}: {stats['total']} → {stats['kept']} "
            f"(-{stats['duplicates']} dupes, -{stats['low_quality']} low-q)"
        )
        return clean, stats

    def curate_conversation(self, messages: List[Dict]) -> List[Dict]:
        """Curate conversation history, removing redundant messages."""
        return self.curate_batch(messages, content_key="content", source="conversation")[0]

    def curate_memories(self, memories: List[Dict]) -> List[Dict]:
        """Curate memory entries from knowledge vault."""
        return self.curate_batch(memories, content_key="value", source="memory", min_quality=0.4)[0]

    def get_stats(self) -> Dict:
        """Get curation statistics."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            total_hashes = conn.execute("SELECT COUNT(*) as c FROM seen_hashes").fetchone()["c"]
            recent = conn.execute(
                "SELECT * FROM curation_stats ORDER BY id DESC LIMIT 10"
            ).fetchall()
            return {
                "unique_hashes": total_hashes,
                "recent_runs": [dict(r) for r in recent],
            }

    def cleanup_old(self, days: int = 30):
        """Remove old hashes to prevent unbounded growth."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        with sqlite3.connect(str(self.db_path)) as conn:
            deleted = conn.execute(
                "DELETE FROM seen_hashes WHERE first_seen < ? AND count = 1", (cutoff,)
            ).rowcount
            logger.info(f"Curator cleanup: removed {deleted} old single-use hashes")
            return deleted
