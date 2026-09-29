"""
Quality Judge - heuristic response and code quality scoring for SuperNEXUS v2.

Scores responses on relevance/completeness/accuracy/clarity and code on
readability/efficiency/correctness/coverage. No LLM calls required.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QualityScore:
    relevance: float = 0.0
    completeness: float = 0.0
    accuracy: float = 0.0
    clarity: float = 0.0
    overall: float = 0.0
    feedback: str = ""


@dataclass
class CodeQualityScore:
    readability: float = 0.0
    efficiency: float = 0.0
    correctness: float = 0.0
    test_coverage: float = 0.0
    overall: float = 0.0
    suggestions: list[str] = field(default_factory=list)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _keyword_overlap(query: str, response: str) -> float:
    q_words = set(re.findall(r"\w{3,}", query.lower()))
    r_words = set(re.findall(r"\w{3,}", response.lower()))
    if not q_words:
        return 0.5
    return len(q_words & r_words) / len(q_words)


class QualityJudge:
    """Heuristic quality judge for responses and code."""

    def __init__(self) -> None:
        self._history: list[dict] = []

    # --- Response scoring ---

    def judge_response(
        self,
        query: str,
        response: str,
        context: Optional[str] = None,
    ) -> QualityScore:
        q_len = max(len(query.split()), 1)
        r_len = len(response.split())

        # Relevance: keyword overlap + length ratio
        kw = _keyword_overlap(query, response)
        ideal_ratio = 3.0
        ratio_score = _clamp(1.0 - abs(r_len / max(q_len, 1) - ideal_ratio) / ideal_ratio)
        relevance = _clamp(0.5 * kw + 0.5 * ratio_score)

        # Completeness: coverage of query terms, length adequacy
        completeness = _clamp(
            kw * 0.6 + _clamp(r_len / max(q_len * 2, 1)) * 0.4
        )

        # Accuracy: heuristic - no obvious contradictions
        sentences = re.split(r"[.!?]+", response)
        contradictions = 0
        lower_resp = response.lower()
        contradiction_markers = ["however", "but actually", "contrary to", "not true"]
        for marker in contradiction_markers:
            if marker in lower_resp:
                contradictions += 1
        accuracy = _clamp(1.0 - 0.15 * contradictions)

        # Clarity: structure
        has_headers = bool(re.search(r"^#{1,6}\s", response, re.MULTILINE))
        has_lists = bool(re.search(r"^[-*]\s", response, re.MULTILINE))
        has_code = bool(re.search(r"```", response))
        has_paragraphs = response.count("\n\n") >= 1
        structure_bonus = (
            (0.15 if has_headers else 0)
            + (0.15 if has_lists else 0)
            + (0.1 if has_code else 0)
            + (0.1 if has_paragraphs else 0)
        )
        clarity = _clamp(0.4 + structure_bonus)

        overall = _clamp(
            0.3 * relevance + 0.3 * completeness + 0.2 * accuracy + 0.2 * clarity
        )

        feedback_parts = []
        if relevance < 0.4:
            feedback_parts.append("Low keyword relevance to query.")
        if completeness < 0.4:
            feedback_parts.append("Response may be too brief or incomplete.")
        if accuracy < 0.7:
            feedback_parts.append("Possible contradictions detected.")
        if clarity < 0.5:
            feedback_parts.append("Could benefit from better structure (headers, lists).")
        feedback = " ".join(feedback_parts) if feedback_parts else "Good quality response."

        score = QualityScore(
            relevance=round(relevance, 3),
            completeness=round(completeness, 3),
            accuracy=round(accuracy, 3),
            clarity=round(clarity, 3),
            overall=round(overall, 3),
            feedback=feedback,
        )

        self._record("response", {
            "overall": overall,
            "relevance": relevance,
            "completeness": completeness,
        })
        return score

    # --- Code scoring ---

    def judge_code(
        self,
        filepath: str,
        code: str,
        tests_pass: Optional[bool] = None,
    ) -> CodeQualityScore:
        lines = code.splitlines()
        suggestions: list[str] = []

        # Readability
        has_docstring = bool(re.search(r'.*?""".*?"""', code, re.DOTALL))
        avg_line_len = sum(len(l) for l in lines) / max(len(lines), 1)
        line_score = _clamp(1.0 - (avg_line_len - 80) / 80) if avg_line_len > 80 else 1.0
        readability = _clamp(
            (0.4 if has_docstring else 0.1) + 0.6 * line_score
        )
        if not has_docstring:
            suggestions.append("Add docstrings to improve readability.")

        # Efficiency - heuristic: nesting depth
        max_nesting = 0
        for line in lines:
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            max_nesting = max(max_nesting, indent // 4)
        efficiency = _clamp(1.0 - max_nesting * 0.1)
        if max_nesting > 3:
            suggestions.append("High nesting depth - consider refactoring.")

        # Correctness - type hints, error handling, syntax
        has_type_hints = bool(re.search(r"def \w+\(.*:\s*\w+", code))
        has_error_handling = bool(re.search(r"(try|except|raise)", code))
        syntax_ok = True
        try:
            ast.parse(code)
        except SyntaxError:
            syntax_ok = False
            suggestions.append("Fix syntax errors.")

        correctness = _clamp(
            (0.3 if has_type_hints else 0.05)
            + (0.3 if has_error_handling else 0.05)
            + (0.4 if syntax_ok else 0.0)
        )
        if not has_type_hints:
            suggestions.append("Add type hints for better correctness signals.")
        if not has_error_handling:
            suggestions.append("Add error handling (try/except or raises).")

        # Naming conventions
        bad_names = re.findall(r"[a-z][A-Z][a-zA-Z]*\s*=", code)
        if len(bad_names) > 3:
            readability *= 0.9
            suggestions.append("Variable naming may be inconsistent (camelCase in Python).")

        # Test coverage
        if tests_pass is True:
            test_coverage = 1.0
        elif tests_pass is False:
            test_coverage = 0.0
            suggestions.append("Tests are failing.")
        else:
            has_test_pattern = bool(re.search(r"test_|assert |unittest|pytest", code))
            test_coverage = 0.5 if has_test_pattern else 0.3

        overall = _clamp(
            0.25 * readability + 0.25 * efficiency
            + 0.30 * correctness + 0.20 * test_coverage
        )

        score = CodeQualityScore(
            readability=round(readability, 3),
            efficiency=round(efficiency, 3),
            correctness=round(correctness, 3),
            test_coverage=round(test_coverage, 3),
            overall=round(overall, 3),
            suggestions=suggestions,
        )

        self._record("code", {"overall": overall, "filepath": filepath})
        return score

    # --- Batch ---

    def batch_judge(self, pairs: list[dict]) -> list[QualityScore]:
        results: list[QualityScore] = []
        for pair in pairs:
            q = pair.get("query", "")
            r = pair.get("response", "")
            ctx = pair.get("context")
            results.append(self.judge_response(q, r, ctx))
        return results

    # --- Stats & Trend ---

    def get_stats(self) -> dict:
        total = len(self._history)
        if total == 0:
            return {"total_judged": 0, "avg_overall": 0, "by_area": {}}
        avg = sum(h["overall"] for h in self._history) / total
        by_area: dict[str, dict] = {}
        area_counts: dict[str, int] = defaultdict(int)
        area_sums: dict[str, float] = defaultdict(float)
        for h in self._history:
            a = h.get("area", "unknown")
            area_counts[a] += 1
            area_sums[a] += h["overall"]
        for a in area_counts:
            by_area[a] = {
                "count": area_counts[a],
                "avg": round(area_sums[a] / area_counts[a], 3),
            }
        return {
            "total_judged": total,
            "avg_overall": round(avg, 3),
            "by_area": by_area,
        }

    def get_trend(self, area: Optional[str] = None) -> list[dict]:
        filtered = self._history
        if area:
            filtered = [h for h in filtered if h.get("area") == area]
        return [
            {"overall": h["overall"], "area": h.get("area", "unknown")}
            for h in filtered[-50:]
        ]

    # --- Internal ---

    def _record(self, area: str, data: dict) -> None:
        self._history.append({"area": area, **data})


# --- Singleton ---

_judge_instance: Optional[QualityJudge] = None


def get_judge() -> QualityJudge:
    global _judge_instance
    if _judge_instance is None:
        _judge_instance = QualityJudge()
    return _judge_instance
