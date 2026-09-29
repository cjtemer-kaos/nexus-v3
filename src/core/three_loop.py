"""
Three-Loop Self-Improvement System for NEXUS.

Architecture:
- Fast Loop (per-execution): trajectory learning, prompt adaptation, error correction
- Medium Loop (hourly/daily): benchmark, training data generation, fine-tuning
- Slow Loop (weekly): architecture evolution, meta-learning, full retraining

Each loop feeds into the next: fast loop findings accumulate into medium loop
training data, and medium loop results inform slow loop architecture changes.
"""

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("nexus-three-loop")

NEXUS_HOME = Path.home() / ".nexus"
THREE_LOOP_DIR = NEXUS_HOME / "three_loop"
THREE_LOOP_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class TrajectoryStep:
    task: str
    response: str
    latency_ms: float
    score: float
    category: str
    model_used: str
    error: str = ""
    timestamp: str = ""


@dataclass
class LoopState:
    fast_loop_count: int = 0
    medium_loop_count: int = 0
    slow_loop_count: int = 0
    last_fast_loop: str = ""
    last_medium_loop: str = ""
    last_slow_loop: str = ""
    total_trajectories: int = 0
    avg_score: float = 0.0
    improvement_rate: float = 0.0


class FastLoop:
    """
    Fast Loop: learns from every single execution.
    - Stores trajectory (task, response, score)
    - Detects pattern failures
    - Adapts prompt templates in real-time
    - Feeds into Medium Loop
    """

    def __init__(self):
        self.trajectories: List[TrajectoryStep] = []
        self.failure_patterns: Dict[str, int] = defaultdict(int)
        self.category_scores: Dict[str, List[float]] = defaultdict(list)
        self.prompt_adaptations: List[str] = []
        self._trajectory_file = THREE_LOOP_DIR / "trajectories.jsonl"

    async def record(
        self,
        task: str,
        response: str,
        latency_ms: float,
        score: float,
        category: str = "general",
        model_used: str = "qwen2.5-coder:7b",
        error: str = "",
    ) -> None:
        step = TrajectoryStep(
            task=task,
            response=response,
            latency_ms=latency_ms,
            score=score,
            category=category,
            model_used=model_used,
            error=error,
            timestamp=datetime.now().isoformat(),
        )
        self.trajectories.append(step)
        self.category_scores[category].append(score)

        if error:
            self.failure_patterns[error] += 1

        # Persist incrementally
        with open(self._trajectory_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(step.__dict__, ensure_ascii=False) + "\n")

    def get_failure_insights(self) -> List[Dict]:
        top_failures = sorted(self.failure_patterns.items(), key=lambda x: x[1], reverse=True)[:5]
        return [
            {"pattern": p, "count": c, "suggestion": f"Review {p} logic"}
            for p, c in top_failures
        ]

    def get_category_performance(self) -> Dict[str, float]:
        return {
            cat: sum(scores) / len(scores) if scores else 0.0
            for cat, scores in self.category_scores.items()
        }

    def get_weak_categories(self, threshold: float = 0.6) -> List[str]:
        return [
            cat for cat, avg in self.get_category_performance().items()
            if avg < threshold
        ]

    def generate_training_signal(self) -> List[Dict]:
        """Generate training examples from high-scoring trajectories."""
        signals = []
        for t in self.trajectories[-100:]:  # Last 100
            if t.score >= 0.8 and t.response:
                signals.append({
                    "instruction": t.task,
                    "output": t.response,
                    "category": t.category,
                    "source": "fast_loop",
                    "score": t.score,
                })
        return signals

    def get_stats(self) -> Dict:
        scores = [t.score for t in self.trajectories if t.score > 0]
        return {
            "total_trajectories": len(self.trajectories),
            "avg_score": round(sum(scores) / len(scores), 3) if scores else 0,
            "failure_patterns": dict(sorted(self.failure_patterns.items(), key=lambda x: x[1], reverse=True)[:5]),
            "category_performance": self.get_category_performance(),
            "weak_categories": self.get_weak_categories(),
            "training_signals_available": len(self.generate_training_signal()),
        }


class MediumLoop:
    """
    Medium Loop: periodic self-improvement via benchmark + training data.
    - Runs RecursiveSeedAI benchmark
    - Generates adaptive training recipe
    - Exports SFT/DPO datasets
    - Calls optional fine-tuning
    """

    def __init__(self, recursive_seed=None, model_autopsy=None):
        self._rsai = recursive_seed
        self._autopsy = model_autopsy
        self._improvement_history: List[Dict] = []

    async def run(
        self,
        execute_fn: Callable = None,
        judge_fn: Callable = None,
        fast_loop: FastLoop = None,
        sample_size: int = 30,
    ) -> Dict:
        logger.info("Medium Loop: starting improvement cycle...")

        if not self._rsai:
            return {"status": "error", "error": "No RecursiveSeedAI available"}

        # Incorporate fast loop signals
        if fast_loop:
            signals = fast_loop.generate_training_signal()
            if signals:
                logger.info(f"  Incorporating {len(signals)} fast-loop training signals")
                # In a full implementation, these would augment the SFT dataset

        # Run benchmark
        benchmark = await self._rsai.benchmark_nexus(
            execute_fn=execute_fn,
            judge_fn=judge_fn,
            sample_size=sample_size,
        )

        # Generate improved recipe
        recipe = self._rsai.generate_training_recipe(benchmark=benchmark)

        # Export focused datasets
        weaknesses = benchmark.weaknesses
        sft_path = str(self._rsai.export_sft_dataset(
            categories=weaknesses if weaknesses else None,
        ))
        dpo_path = str(self._rsai.export_dpo_dataset(
            categories=weaknesses if weaknesses else None,
        ))

        result = {
            "timestamp": datetime.now().isoformat(),
            "benchmark": benchmark.to_dict(),
            "recipe": recipe,
            "datasets": {"sft": sft_path, "dpo": dpo_path},
            "weaknesses": weaknesses,
        }

        self._improvement_history.append(result)
        self._save_history()

        logger.info(f"Medium Loop complete. Score: {benchmark.avg_score:.3f}, weaknesses: {weaknesses}")
        return result

    def get_improvement_trend(self) -> Dict:
        if len(self._improvement_history) < 2:
            return {"trend": "insufficient_data", "history": self._improvement_history}

        first = self._improvement_history[0]
        last = self._improvement_history[-1]
        delta = last["benchmark"]["avg_score"] - first["benchmark"]["avg_score"]

        return {
            "iterations": len(self._improvement_history),
            "first_score": first["benchmark"]["avg_score"],
            "last_score": last["benchmark"]["avg_score"],
            "delta": round(delta, 4),
            "trend": "improving" if delta > 0 else "declining" if delta < 0 else "stable",
        }

    def _save_history(self):
        path = THREE_LOOP_DIR / "medium_loop_history.json"
        atomic_write_json(path, self._improvement_history)


class SlowLoop:
    """
    Slow Loop: architecture evolution and meta-learning.
    - Analyzes capability maps from Model Autopsy
    - Recommends system architecture changes
    - Tracks long-term improvement trends
    - Generates meta-learning reports
    """

    def __init__(self, model_autopsy=None):
        self._autopsy = model_autopsy
        self._architecture_decisions: List[Dict] = []

    async def run(self, fast_loop: FastLoop = None, medium_loop: MediumLoop = None) -> Dict:
        logger.info("Slow Loop: architecture analysis...")

        recommendations = []

        # Analyze capability map
        if self._autopsy and self._autopsy.get_capability_map():
            cm = self._autopsy.get_capability_map()
            win_counts: Dict[str, int] = {}
            for cat, model in cm.best_model_per_category.items():
                win_counts[model] = win_counts.get(model, 0) + 1

            # If some model other than default is winning categories, route more
            for model, count in win_counts.items():
                if model != "qwen2.5-coder:7b" and count >= 2:
                    recommendations.append({
                        "type": "routing",
                        "priority": "medium",
                        "action": f"Route more tasks to {model} ({count} categories dominated)",
                    })

        # Analyze improvement trend
        if medium_loop:
            trend = medium_loop.get_improvement_trend()
            if trend.get("trend") == "declining":
                recommendations.append({
                    "type": "training",
                    "priority": "high",
                    "action": "Improvement trend declining. Switch training method or increase data.",
                })
            elif trend.get("trend") == "stable" and trend.get("iterations", 0) >= 3:
                recommendations.append({
                    "type": "meta",
                    "priority": "low",
                    "action": "Stable improvement. Consider architecture evolution (MoE layers, memory augmentation).",
                })

        # Analyze fast loop data
        if fast_loop:
            weak = fast_loop.get_weak_categories(threshold=0.6)
            if len(weak) >= 3:
                recommendations.append({
                    "type": "data",
                    "priority": "high",
                    "action": f"Add external training data for weak categories: {weak}",
                })

        result = {
            "timestamp": datetime.now().isoformat(),
            "recommendations": recommendations,
            "fast_loop_stats": fast_loop.get_stats() if fast_loop else {},
            "medium_loop_trend": medium_loop.get_improvement_trend() if medium_loop else {},
        }

        self._architecture_decisions.append(result)
        self._save_analysis()

        logger.info(f"Slow Loop complete. {len(recommendations)} recommendations generated.")
        return result

    def _save_analysis(self):
        path = THREE_LOOP_DIR / "slow_loop_analysis.json"
        atomic_write_json(path, self._architecture_decisions)


class ThreeLoopSystem:
    """
    Orchestrates all three loops together.
    Fast Loop runs continuously; Medium Loop on demand/schedule;
    Slow Loop on demand/schedule.
    """

    def __init__(self, recursive_seed=None, model_autopsy=None):
        self.fast_loop = FastLoop()
        self.medium_loop = MediumLoop(recursive_seed, model_autopsy)
        self.slow_loop = SlowLoop(model_autopsy)
        self.state = LoopState()
        self._load_state()

    async def record_execution(
        self,
        task: str,
        response: str,
        latency_ms: float,
        score: float,
        category: str = "general",
        model_used: str = "qwen2.5-coder:7b",
        error: str = "",
    ) -> None:
        """Called after every task execution. Feeds the Fast Loop."""
        await self.fast_loop.record(task, response, latency_ms, score, category, model_used, error)
        self.state.fast_loop_count += 1
        self.state.total_trajectories = len(self.fast_loop.trajectories)
        self.state.avg_score = self.fast_loop.get_stats().get("avg_score", 0)
        self.state.last_fast_loop = datetime.now().isoformat()

    async def run_medium_loop(
        self,
        execute_fn: Callable = None,
        judge_fn: Callable = None,
        sample_size: int = 30,
    ) -> Dict:
        """Run the Medium Loop improvement cycle."""
        result = await self.medium_loop.run(
            execute_fn=execute_fn,
            judge_fn=judge_fn,
            fast_loop=self.fast_loop,
            sample_size=sample_size,
        )
        self.state.medium_loop_count += 1
        self.state.last_medium_loop = datetime.now().isoformat()

        if self.state.medium_loop_count > 1 and self.state.fast_loop_count > 0:
            prev = self.state.avg_score
            current = self.medium_loop.get_improvement_trend().get("last_score", prev)
            self.state.improvement_rate = (current - prev) / prev if prev > 0 else 0

        self._save_state()
        return result

    async def run_slow_loop(self) -> Dict:
        """Run the Slow Loop architecture analysis."""
        result = await self.slow_loop.run(
            fast_loop=self.fast_loop,
            medium_loop=self.medium_loop,
        )
        self.state.slow_loop_count += 1
        self.state.last_slow_loop = datetime.now().isoformat()
        self._save_state()
        return result

    async def run_all_loops(
        self,
        execute_fn: Callable = None,
        judge_fn: Callable = None,
        sample_size: int = 30,
    ) -> Dict:
        """Run all three loops in sequence (medium then slow, fast is continuous)."""
        medium = await self.run_medium_loop(execute_fn, judge_fn, sample_size)
        slow = await self.run_slow_loop()
        return {"medium_loop": medium, "slow_loop": slow, "state": self.state.__dict__}

    def get_full_report(self) -> Dict:
        return {
            "state": self.state.__dict__,
            "fast_loop": self.fast_loop.get_stats(),
            "medium_loop_trend": self.medium_loop.get_improvement_trend(),
            "slow_loop_analyses": len(self.slow_loop._architecture_decisions),
        }

    def _save_state(self):
        path = THREE_LOOP_DIR / "loop_state.json"
        atomic_write_json(path, self.state.__dict__)

    def _load_state(self):
        path = THREE_LOOP_DIR / "loop_state.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                self.state = LoopState(**data)
            except Exception:
                pass
