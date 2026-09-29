"""
Code Absorption Pipeline — scan repos, extract patterns, absorb into memory.
"""
from __future__ import annotations
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AbsorbedPattern:
    name: str
    category: str  # class, function, pattern, config, test
    source_repo: str
    code_snippet: str
    quality_score: float = 0.5
    language: str = "python"
    file_path: str = ""
    line_number: int = 0
    tags: list[str] = field(default_factory=list)
    absorbed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "name": self.name, "category": self.category, "source_repo": self.source_repo,
            "code_snippet": self.code_snippet[:200], "quality_score": round(self.quality_score, 2),
            "language": self.language, "file_path": self.file_path, "line_number": self.line_number,
            "tags": self.tags,
        }


CLASS_PATTERN = re.compile(r"class\s+(\w+)(?:\(.*?\))?:")
FUNC_PATTERN = re.compile(r"(?:async\s+)?def\s+(\w+)\s*\(")
IMPORT_PATTERN = re.compile(r"^(?:from|import)\s+[\w.]+")


class PatternExtractor:
    """Extract patterns from source files using regex + AST-lite."""

    def extract_from_python(self, content: str, file_path: str = "") -> list[dict]:
        patterns = []
        lines = content.split("\n")
        for i, line in enumerate(lines):
            cm = CLASS_PATTERN.match(line.strip())
            if cm:
                class_name = cm.group(1)
                snippet = "\n".join(lines[i : min(i + 15, len(lines))])
                patterns.append({"name": class_name, "category": "class",
                               "code_snippet": snippet, "line_number": i + 1})
                continue
            fm = FUNC_PATTERN.match(line.strip())
            if fm:
                func_name = fm.group(1)
                if func_name.startswith("_"):
                    continue
                snippet = "\n".join(lines[i : min(i + 10, len(lines))])
                patterns.append({"name": func_name, "category": "function",
                               "code_snippet": snippet, "line_number": i + 1})
        return patterns

    def extract_from_js_ts(self, content: str, file_path: str = "") -> list[dict]:
        patterns = []
        lines = content.split("\n")
        # Class detection
        for i, line in enumerate(lines):
            m = re.match(r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", line.strip())
            if m:
                snippet = "\n".join(lines[i : min(i + 15, len(lines))])
                patterns.append({"name": m.group(1), "category": "class",
                               "code_snippet": snippet, "line_number": i + 1})
                continue
            m = re.match(r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", line.strip())
            if m:
                snippet = "\n".join(lines[i : min(i + 10, len(lines))])
                patterns.append({"name": m.group(1), "category": "function",
                               "code_snippet": snippet, "line_number": i + 1})
                continue
            # Arrow function / method
            m = re.match(r"(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?.*?\)?\s*=>", line.strip())
            if m:
                snippet = "\n".join(lines[i : min(i + 10, len(lines))])
                patterns.append({"name": m.group(1), "category": "function",
                               "code_snippet": snippet, "line_number": i + 1})
        return patterns


class CodeAbsorber:
    """Scan a repo directory and absorb code patterns into memory."""

    def __init__(self, brain_store=None):
        self._patterns: list[AbsorbedPattern] = []
        self._brain_store = brain_store  # callable(content) to store in brain
        self._extractor = PatternExtractor()

    def scan_repo(self, repo_path: str, repo_name: str = "") -> list[AbsorbedPattern]:
        """Scan a repo directory and extract patterns."""
        root = Path(repo_path)
        if not root.exists():
            raise FileNotFoundError(f"Repo not found: {repo_path}")
        name = repo_name or root.name
        patterns = []

        for file_path in root.rglob("*"):
            if not file_path.is_file():
                continue
            ext = file_path.suffix.lower()
            if ext not in (".py", ".js", ".ts", ".jsx", ".tsx"):
                continue
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if ext == ".py":
                extracted = self._extractor.extract_from_python(content, str(file_path))
            else:
                extracted = self._extractor.extract_from_js_ts(content, str(file_path))

            for p in extracted:
                pattern = AbsorbedPattern(
                    name=p["name"],
                    category=p["category"],
                    source_repo=name,
                    code_snippet=p["code_snippet"],
                    file_path=str(file_path),
                    line_number=p["line_number"],
                    language="python" if ext == ".py" else "javascript",
                    quality_score=self._score_quality(p["code_snippet"]),
                )
                patterns.append(pattern)

        self._patterns.extend(patterns)
        logger.info(f"CodeAbsorber: scanned {name}, found {len(patterns)} patterns")
        return patterns

    def absorb(self, patterns: list[AbsorbedPattern] | None = None) -> int:
        """Absorb patterns into brain memory."""
        targets = patterns or self._patterns
        count = 0
        for p in targets:
            if self._brain_store:
                self._brain_store(
                    f"Pattern [{p.category}] {p.name} from {p.source_repo}: {p.code_snippet[:200]}",
                )
            count += 1
        return count

    def _score_quality(self, snippet: str) -> float:
        """Score code quality 0-1 based on length, complexity indicators."""
        lines = snippet.strip().split("\n")
        if len(lines) < 2:
            return 0.3
        score = 0.5
        score += min(len(lines) / 30.0, 0.3)
        if "return" in snippet or "yield" in snippet:
            score += 0.1
        if "async" in snippet or "await" in snippet:
            score += 0.05
        if re.search(r"def\s+\w+|class\s+\w+", snippet):
            score += 0.05
        return min(score, 1.0)

    def status(self) -> dict:
        categories = {}
        for p in self._patterns:
            categories[p.category] = categories.get(p.category, 0) + 1
        return {
            "total_patterns": len(self._patterns),
            "categories": categories,
            "avg_quality": round(
                sum(p.quality_score for p in self._patterns) / max(len(self._patterns), 1), 2
            ),
        }
