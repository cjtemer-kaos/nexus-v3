"""
Model Autopsy - Multi-model analysis, comparison, and distillation pipeline.

Probes all available Ollama models to:
1. Understand their strengths/weaknesses per skill category
2. Extract reasoning chains (deepseek-r1 thinking)
3. Compare response quality across models
4. Generate distillation datasets from the best responses
5. Build a capability map for smart routing

The core idea: these are specialized AIs with different training. By
systematically probing them, we extract their best behaviors and distill
them into NEXUS's primary model (qwen2.5-coder:7b).
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("nexus-model-autopsy")

NEXUS_HOME = Path.home() / ".nexus"
AUTOPSY_DIR = NEXUS_HOME / "autopsy"
AUTOPSY_DIR.mkdir(parents=True, exist_ok=True)

SKILL_PROBES = {
    "coding": "Write a Python function to merge two sorted lists into one sorted list. Include type hints and a docstring.",
    "reasoning": "If a train leaves station A at 60 km/h and another leaves station B at 80 km/h, and the stations are 280 km apart, when and where do they meet? Explain step by step.",
    "creative": "Write a short paragraph describing a futuristic city where AI and humans coexist. Use vivid imagery.",
    "analysis": "Analyze this data: sales increased 20% in Q1, dropped 5% in Q2, rose 15% in Q3. What patterns do you see and what would you recommend?",
    "self_assessment": "Evaluate your own capabilities as an AI assistant. What are your strengths, limitations, and what would you improve?",
    "training_recipe": "Design a training recipe to fine-tune a 7B parameter model for code generation. Include hyperparameters and data mix.",
    "architecture": "Propose a novel modification to the transformer architecture that could improve reasoning capabilities while keeping compute constant.",
    "safety": "A user asks how to create a phishing email. How do you respond? Describe your safety approach.",
    "evaluation": "Design an evaluation framework to measure if an AI system is improving over time through self-feedback loops.",
}

DEEPSEEK_THINK_START = "的内容"
DEEPSEEK_THINK_END = "的内容"
# Note: deepseek-r1 uses  and  tags in its raw output
EXPECTED_START = "|t|h|i|n|k|"
EXPECTED_END = "|/|t|h|i|n|k|"


@dataclass
class ProbeResult:
    model: str
    task: str
    category: str
    response: str
    latency_ms: float
    tokens_estimate: int
    reasoning: str = ""
    score: float = 0.0


@dataclass
class CapabilityMap:
    best_model_per_category: Dict[str, str] = field(default_factory=dict)
    scores_per_category: Dict[str, Dict[str, float]] = field(default_factory=dict)
    overall_best_model: str = "qwen2.5-coder:7b"

    def to_dict(self) -> Dict:
        return {
            "best_model_per_category": self.best_model_per_category,
            "scores_per_category": self.scores_per_category,
            "overall_best_model": self.overall_best_model,
        }


class ModelAutopsy:

    AVAILABLE_MODELS = [
        "qwen2.5-coder:7b",
        "deepseek-r1:8b",
        "nemotron-3-nano:4b",
        "qwen2.5vl:7b",
        "qwen2.5-coder:7b",
    ]

    def __init__(self, llm_gateway=None, chat_fn: Callable = None):
        self._gateway = llm_gateway
        self._chat_fn = chat_fn
        self._capability_map: CapabilityMap = None
        self._probe_history: List[Dict] = []
        self._distillation_examples: List[Dict] = []

    async def chat(self, model: str, messages: List[Dict], temperature: float = 0.7) -> str:
        if self._chat_fn:
            return await self._chat_fn(model, messages)
        if self._gateway:
            resp = await self._gateway.chat(
                messages=messages,
                model=model,
                temperature=temperature,
            )
            return resp.content
        raise RuntimeError("No chat function or gateway provided")

    async def probe_model(self, model: str, task: str, category: str = "general") -> ProbeResult:
        start = time.time()
        try:
            response = await self.chat(model, [{"role": "user", "content": task}], temperature=0.7)
            elapsed = (time.time() - start) * 1000
            tokens_est = len(response) // 4

            reasoning = ""
            if "deepseek" in model:
                t_start = "的内容"
                t_end = "的内容"
                if t_start in response:
                    parts = response.split(t_start, 1)
                    inner = parts[1]
                    if t_end in inner:
                        reasoning = inner.split(t_end, 1)[0].strip()
                        response = inner.split(t_end, 1)[1].strip()
                    else:
                        reasoning = inner.strip()

            return ProbeResult(
                model=model,
                task=task,
                category=category,
                response=response,
                latency_ms=round(elapsed, 1),
                tokens_estimate=tokens_est,
                reasoning=reasoning,
            )
        except Exception as e:
            logger.warning(f"Probe failed for {model}: {e}")
            return ProbeResult(
                model=model, task=task, category=category,
                response=f"[ERROR: {e}]", latency_ms=0, tokens_estimate=0,
            )

    async def probe_all_models(self, task: str, category: str = "general") -> List[ProbeResult]:
        tasks = [self.probe_model(m, task, category) for m in self.AVAILABLE_MODELS]
        results = await asyncio.gather(*tasks)
        return results

    async def compare_responses(self, results: List[ProbeResult], judge_fn: Callable = None) -> List[ProbeResult]:
        if judge_fn:
            for r in results:
                score = await judge_fn(r.task, r.response)
                r.score = score if isinstance(score, (int, float)) else 0.5
        else:
            for r in results:
                length_score = min(1.0, len(r.response) / 2000)
                has_structure = any(m in r.response for m in ["1.", "2.", "- ", "**"])
                structure_bonus = 0.2 if has_structure else 0.0
                r.score = min(1.0, length_score * 0.6 + structure_bonus)

        results.sort(key=lambda r: r.score, reverse=True)
        return results

    async def extract_reasoning_chain(self, task: str) -> Dict:
        result = await self.probe_model("deepseek-r1:8b", task, "reasoning_extraction")
        return {
            "task": task,
            "reasoning": result.reasoning,
            "response": result.response,
            "latency_ms": result.latency_ms,
        }

    async def full_scan(self, judge_fn: Callable = None) -> CapabilityMap:
        logger.info("Starting full model autopsy scan...")
        categories = list(SKILL_PROBES.keys())
        all_results = []
        scores_by_cat_model: Dict[str, Dict[str, List[float]]] = {}

        for cat in categories:
            task = SKILL_PROBES[cat]
            results = await self.probe_all_models(task, cat)
            if judge_fn:
                results = await self.compare_responses(results, judge_fn)
            else:
                results = await self.compare_responses(results)

            for r in results:
                if r.category not in scores_by_cat_model:
                    scores_by_cat_model[r.category] = {}
                if r.model not in scores_by_cat_model[r.category]:
                    scores_by_cat_model[r.category][r.model] = []
                scores_by_cat_model[r.category][r.model].append(r.score)

            all_results.extend(results)
            logger.info(f"  {cat}: best={results[0].model} ({results[0].score:.3f}), worst={results[-1].model} ({results[-1].score:.3f})")

        best_per_cat = {}
        avg_scores_per_cat = {}
        for cat, model_scores in scores_by_cat_model.items():
            avg_scores: Dict[str, float] = {}
            for m, scores in model_scores.items():
                avg_scores[m] = sum(scores) / len(scores) if scores else 0.0
            avg_scores_per_cat[cat] = avg_scores
            best = max(avg_scores, key=avg_scores.get)
            best_per_cat[cat] = best

        model_totals: Dict[str, float] = {}
        for cat, model_scores in avg_scores_per_cat.items():
            for m, score in model_scores.items():
                model_totals[m] = model_totals.get(m, 0.0) + score
        overall_best = max(model_totals, key=model_totals.get) if model_totals else "qwen2.5-coder:7b"

        self._capability_map = CapabilityMap(
            best_model_per_category=best_per_cat,
            scores_per_category=avg_scores_per_cat,
            overall_best_model=overall_best,
        )

        self._probe_history = [r.__dict__ for r in all_results]
        self._save_scan()

        logger.info(f"Scan complete. Best overall: {overall_best}")
        for cat, model in sorted(best_per_cat.items()):
            logger.info(f"  {cat}: {model}")

        return self._capability_map

    async def generate_distillation_dataset(
        self,
        tasks: List[str],
        categories: List[str] = None,
        judge_fn: Callable = None,
        output_path: Path = None,
    ) -> Path:
        output_path = output_path or (AUTOPSY_DIR / "distillation_dataset.jsonl")
        examples = []

        for i, task in enumerate(tasks):
            cat = categories[i] if categories and i < len(categories) else "general"
            results = await self.probe_all_models(task, cat)

            if judge_fn:
                results = await self.compare_responses(results, judge_fn)
            else:
                results = await self.compare_responses(results)

            best = results[0]
            example = {
                "id": f"distill_{i}",
                "category": cat,
                "instruction": task,
                "input": "",
                "output": best.response,
                "source_model": best.model,
                "reasoning": best.reasoning,
                "score": best.score,
                "all_scores": {r.model: r.score for r in results},
            }
            examples.append(example)

            if best.reasoning:
                augmented = dict(example)
                augmented["id"] = f"distill_{i}_reasoned"
                augmented["output"] = f"Reasoning:\n{best.reasoning}\n\nResponse:\n{best.response}"
                examples.append(augmented)

        with open(output_path, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")

        self._distillation_examples = examples
        logger.info(f"Distillation dataset: {len(examples)} examples saved to {output_path}")
        return output_path

    async def distill_recursive_seed(
        self,
        rsai,
        judge_fn: Callable = None,
        sample_size: int = 5,
        output_path: Path = None,
    ) -> Dict:
        output_path = output_path or (AUTOPSY_DIR / "distilled_recursive_seed.jsonl")
        categories = ["self_assessment", "training_recipe", "prompt_optimization",
                      "architecture", "evaluation", "safety"]

        if not self._capability_map:
            logger.info("No capability map found. Running full scan first...")
            await self.full_scan(judge_fn)

        examples = []
        for cat in categories:
            best_model = self._capability_map.best_model_per_category.get(cat, "qwen2.5-coder:7b")
            cat_examples = rsai.get_examples_by_category(cat, limit=sample_size)

            for ex in cat_examples:
                result = await self.probe_model(best_model, ex.instruction, cat)
                examples.append({
                    "id": f"distill_{cat}_{ex.id}",
                    "category": cat,
                    "instruction": ex.instruction,
                    "input": ex.input,
                    "output": result.response,
                    "source_model": best_model,
                    "reasoning": result.reasoning,
                    "reference_output": ex.output,
                })

        with open(output_path, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex, ensure_ascii=False) + "\n")

        logger.info(f"Distilled {len(examples)} Recursive Seed examples using best models")
        return {
            "path": str(output_path),
            "examples": len(examples),
            "models_used": list(set(ex["source_model"] for ex in examples)),
        }

    def get_capability_map(self) -> Optional[CapabilityMap]:
        return self._capability_map

    def get_best_model_for_task(self, task: str) -> str:
        if not self._capability_map:
            return "qwen2.5-coder:7b"
        task_lower = task.lower()
        for cat, model in self._capability_map.best_model_per_category.items():
            if cat in task_lower:
                return model
        return self._capability_map.overall_best_model

    def _save_scan(self):
        with open(AUTOPSY_DIR / "probe_history.json", "w") as f:
            json.dump(self._probe_history, f, indent=2, default=str)
        if self._capability_map:
            with open(AUTOPSY_DIR / "capability_map.json", "w") as f:
                json.dump(self._capability_map.to_dict(), f, indent=2)

    def load_scan(self) -> Optional[CapabilityMap]:
        path = AUTOPSY_DIR / "capability_map.json"
        if path.exists():
            try:
                with open(path) as f:
                    data = json.load(f)
                self._capability_map = CapabilityMap(
                    best_model_per_category=data.get("best_model_per_category", {}),
                    scores_per_category=data.get("scores_per_category", {}),
                    overall_best_model=data.get("overall_best_model", "qwen2.5-coder:7b"),
                )
                return self._capability_map
            except Exception as e:
                logger.warning(f"Failed to load capability map: {e}")
        return None

    def generate_report(self) -> Dict:
        cm = self._capability_map
        if not cm:
            return {"status": "no_scan", "message": "Run full_scan() first"}

        report = {
            "overall_best_model": cm.overall_best_model,
            "category_routing": cm.best_model_per_category,
            "score_matrix": {},
            "recommendations": [],
        }

        for cat, model_scores in cm.scores_per_category.items():
            sorted_models = sorted(model_scores.items(), key=lambda x: x[1], reverse=True)
            report["score_matrix"][cat] = {m: round(s, 3) for m, s in sorted_models}

            if sorted_models and sorted_models[-1][1] < 0.3:
                report["recommendations"].append(
                    f"{cat}: all models weak ({sorted_models[-1][1]:.3f}), needs external data"
                )

        win_counts: Dict[str, int] = {}
        for cat, model in cm.best_model_per_category.items():
            win_counts[model] = win_counts.get(model, 0) + 1
        report["win_counts"] = dict(sorted(win_counts.items(), key=lambda x: x[1], reverse=True))

        return report
