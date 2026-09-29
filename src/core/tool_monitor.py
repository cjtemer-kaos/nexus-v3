"""
F17: Tool Monitoring

Per-tool call tracking, cost calculation, usage frequency, error rates.
"""

import time
from typing import Dict, List
from dataclasses import dataclass
from datetime import datetime
from collections import defaultdict
from src.core.bounded_history import BoundedHistory


@dataclass
class ToolCall:
    tool: str
    timestamp: float
    duration_ms: float
    tokens: int
    success: bool
    error: str = ""
    cost: float = 0.0


class ToolMonitor:
    """Tracks per-tool usage, cost, and performance"""

    # Estimated cost per 1K tokens (local Ollama = $0, but track for reference)
    COST_PER_1K_TOKENS = {
        "local": 0.0,
        "gpt-4": 0.03,
        "gpt-3.5": 0.002,
        "claude": 0.008,
    }

    def __init__(self):
        self._calls = BoundedHistory(maxlen=500)
        self._stats: Dict[str, Dict] = defaultdict(lambda: {
            "total_calls": 0,
            "total_tokens": 0,
            "total_duration_ms": 0,
            "total_errors": 0,
            "total_cost": 0.0,
        })

    def record_call(self, tool: str, duration_ms: float, tokens: int, success: bool, error: str = "", model_type: str = "local"):
        cost = (tokens / 1000) * self.COST_PER_1K_TOKENS.get(model_type, 0.0)
        call = ToolCall(
            tool=tool,
            timestamp=time.time(),
            duration_ms=duration_ms,
            tokens=tokens,
            success=success,
            error=error,
            cost=cost,
        )
        self._calls.append(call)

        # Update stats
        stats = self._stats[tool]
        stats["total_calls"] += 1
        stats["total_tokens"] += tokens
        stats["total_duration_ms"] += duration_ms
        stats["total_cost"] += cost
        if not success:
            stats["total_errors"] += 1

    def get_tool_stats(self, tool: str) -> Dict:
        stats = self._stats.get(tool, {})
        calls = stats.get("total_calls", 0)
        return {
            "tool": tool,
            "total_calls": calls,
            "total_tokens": stats.get("total_tokens", 0),
            "avg_duration_ms": round(stats.get("total_duration_ms", 0) / max(calls, 1), 1),
            "error_rate": round((stats.get("total_errors", 0) / max(calls, 1)) * 100, 1),
            "total_cost": round(stats.get("total_cost", 0), 4),
        }

    def get_all_stats(self) -> Dict:
        return {tool: self.get_tool_stats(tool) for tool in self._stats}

    def get_recent_calls(self, limit: int = 20) -> List[Dict]:
        calls = self._calls.get_all()
        return [
            {
                "tool": c.tool,
                "duration_ms": round(c.duration_ms, 1),
                "tokens": c.tokens,
                "success": c.success,
                "cost": round(c.cost, 4),
                "timestamp": datetime.fromtimestamp(c.timestamp).isoformat(),
            }
            for c in sorted(calls, key=lambda x: x.timestamp, reverse=True)[:limit]
        ]

    def get_summary(self) -> Dict:
        total_calls = sum(s["total_calls"] for s in self._stats.values())
        total_tokens = sum(s["total_tokens"] for s in self._stats.values())
        total_cost = sum(s["total_cost"] for s in self._stats.values())
        total_errors = sum(s["total_errors"] for s in self._stats.values())
        total_duration = sum(s["total_duration_ms"] for s in self._stats.values())

        return {
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "total_errors": total_errors,
            "overall_error_rate": round((total_errors / max(total_calls, 1)) * 100, 1),
            "avg_duration_ms": round(total_duration / max(total_calls, 1), 1),
            "tools_used": len(self._stats),
            "top_tools": sorted(
                [{"tool": t, "calls": s["total_calls"]} for t, s in self._stats.items()],
                key=lambda x: x["calls"], reverse=True,
            )[:5],
        }

    def get_ranking(self) -> List[Dict]:
        """Get tools ranked by usage"""
        return sorted(
            [{"tool": t, "calls": s["total_calls"], "tokens": s["total_tokens"]} for t, s in self._stats.items()],
            key=lambda x: x["calls"], reverse=True,
        )

    def get_cost_summary(self) -> Dict:
        """Get cost summary across all tools"""
        total_tokens = sum(s["total_tokens"] for s in self._stats.values())
        total_cost = sum(s["total_cost"] for s in self._stats.values())
        return {
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 4),
            "by_tool": {t: {"tokens": s["total_tokens"], "cost": round(s["total_cost"], 4)} for t, s in self._stats.items()},
        }
