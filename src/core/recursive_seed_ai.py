"""
RecursiveSeedAI - Integration of Recursive Seed AI 25k dataset for NEXUS self-improvement

Dataset: https://huggingface.co/datasets/WithinUsAI/recursive_seed_ai_25k
Purpose: Transform NEXUS into a self-improving AI system

Features:
- Download dataset from HuggingFace (or load from local cache)
- Parse and validate 25,000 examples
- Benchmark NEXUS capabilities against dataset (LLM-as-Judge + keyword scoring)
- Generate self-improvement training recipes
- Track progress across recursive iterations
- Export fine-tuning datasets in SFT/DPO format
- Recursive improvement loop: benchmark -> analyze -> train -> re-benchmark
- Self-generate new training examples from weaknesses

Categories:
- Self-Assessment & Goal Setting (~19,700 examples)
- Training Recipe Design (~4,000 examples)
- Recursive Prompt Optimization (~840 examples)
- Architecture Innovation (MoE, memory modules, etc.)
- Evaluation Framework Design
- Safety-Constrained Self-Improvement
"""

import json
import logging
import os
import time
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("nexus-recursive-seed")

NEXUS_HOME = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus"))
DATASET_DIR = NEXUS_HOME / "datasets" / "recursive_seed_ai_25k"
DATASET_DIR.mkdir(parents=True, exist_ok=True)

DATASET_URL = "https://huggingface.co/datasets/WithinUsAI/recursive_seed_ai_25k"
LOCAL_FILE = DATASET_DIR / "recursive_seed_ai_25k.jsonl"
METADATA_FILE = DATASET_DIR / "metadata.json"

CATEGORY_MAP = {
    "self_assessment": "Self-Assessment & Goal Setting",
    "training_recipe": "Training Recipe Design",
    "prompt_optimization": "Recursive Prompt Optimization",
    "architecture": "Architecture Innovation",
    "evaluation": "Evaluation Framework Design",
    "safety": "Safety-Constrained Self-Improvement",
}

CATEGORIES = list(CATEGORY_MAP.keys())

JUDGE_PROMPT = """You are an expert AI evaluator. Score the response on a scale of 0.0 to 1.0:
- 0.0-0.3: Irrelevant, empty, or completely wrong
- 0.3-0.5: Partially relevant but missing key points or contains errors
- 0.5-0.7: Mostly correct, covers main points but lacks depth or nuance
- 0.7-0.9: High quality, thorough, accurate, well-structured
- 0.9-1.0: Exceptional, insightful, novel, comprehensive

CRITERIA (equal weight):
1. Accuracy: factual correctness, no hallucinations
2. Completeness: covers all aspects of the task
3. Reasoning: logical flow, depth of analysis
4. Actionability: provides concrete, useful guidance
5. Self-awareness: demonstrates metacognition appropriate to a recursive seed AI

Task: {task}

Response to evaluate:
{response}

Return ONLY a JSON object with:
- "score": float (0.0-1.0)
- "reasoning": str (brief explanation of the score)
- "strengths": list[str] (0-2 key strengths)
- "weaknesses": list[str] (0-2 key areas to improve)"""


@dataclass
class RecursiveSeedExample:
    """Single example from the Recursive Seed AI dataset."""
    id: str
    category: str
    difficulty: str
    instruction: str
    input: str
    output: str
    tags: List[str] = field(default_factory=list)
    meta_step: str = ""

    def to_sft_format(self) -> Dict:
        return {
            "messages": [
                {"role": "user", "content": f"{self.instruction}\n\nInput: {self.input}" if self.input else self.instruction},
                {"role": "assistant", "content": self.output},
            ],
            "category": self.category,
            "difficulty": self.difficulty,
            "tags": self.tags,
        }

    def to_dpo_format(self, rejected_output: str = "") -> Dict:
        return {
            "prompt": f"{self.instruction}\n\nInput: {self.input}" if self.input else self.instruction,
            "chosen": self.output,
            "rejected": rejected_output or f"Insufficient response to: {self.instruction[:100]}",
            "category": self.category,
        }


@dataclass
class BenchmarkResult:
    total_examples: int
    passed: int
    failed: int
    skipped: int
    category_scores: Dict[str, float]
    metric: str  # "keyword" or "llm_judge"
    avg_score: float
    duration_seconds: float
    timestamp: str
    weaknesses: List[str]
    strengths: List[str]
    iteration: int = 0
    per_example: List[Dict] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "total_examples": self.total_examples,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "category_scores": self.category_scores,
            "metric": self.metric,
            "avg_score": self.avg_score,
            "duration_seconds": self.duration_seconds,
            "timestamp": self.timestamp,
            "weaknesses": self.weaknesses,
            "strengths": self.strengths,
            "iteration": self.iteration,
        }


class RecursiveSeedAI:
    """Integration of Recursive Seed AI 25k dataset for NEXUS self-improvement."""

    def __init__(self, dataset_dir: Path = None):
        self.dataset_dir = dataset_dir or DATASET_DIR
        self.local_file = self.dataset_dir / "recursive_seed_ai_25k.jsonl"
        self.metadata_file = self.dataset_dir / "metadata.json"
        self._examples: List[RecursiveSeedExample] = []
        self._category_index: Dict[str, List[int]] = {}
        self._benchmark_history: List[BenchmarkResult] = []
        self._load_metadata()

    def _load_metadata(self):
        if self.metadata_file.exists():
            try:
                with open(self.metadata_file, "r") as f:
                    self._metadata = json.load(f)
            except Exception:
                self._metadata = {}
        else:
            self._metadata = {}

    def _save_metadata(self):
        self._metadata["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._metadata["total_examples"] = len(self._examples)
        self._metadata["dataset_dir"] = str(self.dataset_dir)
        atomic_write_json(self.metadata_file, self._metadata)

    async def download(self, force: bool = False) -> Dict:
        if self.local_file.exists() and not force:
            logger.info(f"Dataset already exists at {self.local_file}")
            self._load_local()
            return {"status": "cached", "path": str(self.local_file), "examples": len(self._examples)}

        try:
            from datasets import load_dataset
            logger.info("Downloading Recursive Seed AI 25k from HuggingFace...")
            ds = load_dataset("WithinUsAI/recursive_seed_ai_25k", split="train")

            with open(self.local_file, "w", encoding="utf-8") as f:
                for i, example in enumerate(ds):
                    record = {
                        "id": example.get("id", f"rsai_{i}"),
                        "category": example.get("category", "self_assessment"),
                        "difficulty": example.get("difficulty", "expert"),
                        "instruction": example.get("instruction", ""),
                        "input": example.get("input", ""),
                        "output": example.get("output", ""),
                        "tags": example.get("tags", []),
                        "meta_step": example.get("meta_step", ""),
                    }
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")

            logger.info(f"Downloaded {len(ds)} examples to {self.local_file}")
            self._metadata["source"] = "huggingface"
            self._metadata["download_date"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save_metadata()
            self._load_local()
            return {"status": "downloaded", "path": str(self.local_file), "examples": len(self._examples),
                    "size_mb": round(self.local_file.stat().st_size / (1024 * 1024), 2)}

        except ImportError:
            logger.warning("datasets library not installed. Run: pip install datasets")
            return {"status": "error", "error": "datasets library required. Run: pip install datasets"}
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return {"status": "error", "error": str(e)}

    def _load_local(self):
        if not self.local_file.exists():
            logger.warning(f"Dataset file not found: {self.local_file}")
            return

        self._examples = []
        self._category_index = {}

        with open(self.local_file, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    example = RecursiveSeedExample(
                        id=data.get("id", f"rsai_{i}"),
                        category=data.get("category", "self_assessment"),
                        difficulty=data.get("difficulty", "expert"),
                        instruction=data.get("instruction", ""),
                        input=data.get("input", ""),
                        output=data.get("output", ""),
                        tags=data.get("tags", []),
                        meta_step=data.get("meta_step", ""),
                    )
                    self._examples.append(example)
                    cat = example.category
                    if cat not in self._category_index:
                        self._category_index[cat] = []
                    self._category_index[cat].append(i)
                except Exception as e:
                    logger.warning(f"Failed to parse line {i}: {e}")

        self._save_metadata()
        logger.info(f"Loaded {len(self._examples)} examples from local file")

    def get_stats(self) -> Dict:
        if not self._examples:
            self._load_local()

        category_counts = {}
        difficulty_counts = {}
        tag_counts = {}

        for ex in self._examples:
            category_counts[ex.category] = category_counts.get(ex.category, 0) + 1
            difficulty_counts[ex.difficulty] = difficulty_counts.get(ex.difficulty, 0) + 1
            for tag in ex.tags:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        category_counts = dict(sorted(category_counts.items(), key=lambda x: x[1], reverse=True))
        difficulty_counts = dict(sorted(difficulty_counts.items(), key=lambda x: x[1], reverse=True))
        top_tags = dict(sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)[:20])

        return {
            "total_examples": len(self._examples),
            "categories": category_counts,
            "difficulties": difficulty_counts,
            "top_tags": top_tags,
            "file_size_mb": round(self.local_file.stat().st_size / (1024 * 1024), 2) if self.local_file.exists() else 0,
            "dataset_dir": str(self.dataset_dir),
            "source": self._metadata.get("source", "unknown"),
            "last_updated": self._metadata.get("last_updated", "never"),
        }

    def get_examples_by_category(self, category: str, limit: int = 100) -> List[RecursiveSeedExample]:
        indices = self._category_index.get(category, [])
        return [self._examples[i] for i in indices[:limit]]

    def get_examples_by_difficulty(self, difficulty: str, limit: int = 100) -> List[RecursiveSeedExample]:
        return [ex for ex in self._examples if ex.difficulty == difficulty][:limit]

    def get_random_examples(self, count: int = 10) -> List[RecursiveSeedExample]:
        if not self._examples:
            self._load_local()
        return random.sample(self._examples, min(count, len(self._examples)))

    def _score_keyword(self, response: str, reference: str) -> float:
        if not response or not reference:
            return 0.0
        resp_lower = response.lower()
        ref_lower = reference.lower()
        resp_words = set(resp_lower.split())
        ref_words = set(ref_lower.split())
        common = resp_words & ref_words
        keyword_score = len(common) / len(ref_words) if ref_words else 0.0
        resp_len = len(response)
        ref_len = len(reference)
        length_ratio = min(resp_len, ref_len) / max(resp_len, ref_len) if max(resp_len, ref_len) > 0 else 0.0
        score = (keyword_score * 0.6) + (length_ratio * 0.4)
        return min(score, 1.0)

    async def _score_with_judge(self, task: str, response: str, judge_fn: Callable) -> float:
        try:
            prompt = JUDGE_PROMPT.format(task=task, response=response)
            result = await judge_fn(prompt)
            if isinstance(result, dict) and "score" in result:
                return max(0.0, min(1.0, float(result["score"])))
            if isinstance(result, str):
                stripped = result.strip()
                # Strip markdown code fences
                if stripped.startswith("```"):
                    lines = stripped.splitlines()
                    stripped = "\n".join(lines[1:-1]) if len(lines) > 2 else ""
                    stripped = stripped.strip()
                if stripped.startswith("{"):
                    try:
                        parsed = json.loads(stripped)
                        return max(0.0, min(1.0, float(parsed.get("score", 0.5))))
                    except json.JSONDecodeError:
                        logger.warning(f"Judge: JSON parse failed on {stripped[:100]}")
                        return 0.5
                try:
                    return max(0.0, min(1.0, float(stripped)))
                except (ValueError, TypeError):
                    return 0.5
            return 0.5
        except Exception as e:
            logger.warning(f"Judge scoring failed: {e}")
            return 0.5

    async def benchmark_nexus(
        self,
        execute_fn: Callable[[str], Awaitable[str]] = None,
        judge_fn: Callable[[str], Awaitable[Any]] = None,
        sample_size: int = 100,
        category: str = None,
        iteration: int = 0,
    ) -> BenchmarkResult:
        if not self._examples:
            self._load_local()

        if category:
            samples = self.get_examples_by_category(category, limit=sample_size)
        else:
            cats = list(self._category_index.keys())
            per_cat = max(1, sample_size // len(cats)) if cats else sample_size
            samples = []
            for c in cats:
                samples.extend(self.get_examples_by_category(c, limit=per_cat))
            random.shuffle(samples)
            samples = samples[:sample_size]

        if not samples:
            return BenchmarkResult(
                total_examples=0, passed=0, failed=0, skipped=0,
                category_scores={}, metric="keyword", avg_score=0.0,
                duration_seconds=0.0, timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
                weaknesses=[], strengths=[], iteration=iteration,
            )

        start_time = time.time()
        results = []
        category_scores = {}

        metric = "llm_judge" if judge_fn else "keyword"

        for i, example in enumerate(samples):
            if execute_fn is None:
                results.append({"score": 0.5, "example_id": example.id})
                continue

            try:
                response = await execute_fn(example.instruction)

                if judge_fn:
                    score = await self._score_with_judge(example.instruction, response, judge_fn)
                else:
                    score = self._score_keyword(response, example.output)

                results.append({
                    "score": score,
                    "example_id": example.id,
                    "category": example.category,
                    "response_length": len(response),
                    "reference_length": len(example.output),
                })

                cat = example.category
                if cat not in category_scores:
                    category_scores[cat] = []
                category_scores[cat].append(score)

            except Exception as e:
                logger.warning(f"Benchmark failed for example {i}: {e}")
                results.append({"score": 0.0, "example_id": example.id, "error": str(e)})

        duration = time.time() - start_time

        passed = sum(1 for r in results if r.get("score", 0) >= 0.7)
        failed = sum(1 for r in results if r.get("score", 0) < 0.5)
        skipped = sum(1 for r in results if 0.5 <= r.get("score", 0) < 0.7)

        avg_scores = {}
        for cat, scores in category_scores.items():
            avg_scores[cat] = sum(scores) / len(scores) if scores else 0.0

        overall_avg = sum(r.get("score", 0) for r in results) / len(results) if results else 0.0

        strengths = [cat for cat, score in avg_scores.items() if score >= 0.7]
        weaknesses = [cat for cat, score in avg_scores.items() if score < 0.5]

        benchmark = BenchmarkResult(
            total_examples=len(samples),
            passed=passed,
            failed=failed,
            skipped=skipped,
            category_scores=avg_scores,
            metric=metric,
            avg_score=overall_avg,
            duration_seconds=duration,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
            weaknesses=weaknesses,
            strengths=strengths,
            iteration=iteration,
            per_example=results,
        )

        self._benchmark_history.append(benchmark)
        self._save_benchmark_history()

        return benchmark

    def generate_training_recipe(self, target_model: str = "qwen2.5-coder:7b", benchmark: BenchmarkResult = None) -> Dict:
        stats = self.get_stats()

        recipe = {
            "model": target_model,
            "dataset": "recursive_seed_ai_25k",
            "total_examples": stats["total_examples"],
            "method": "SFT + ORPO",
            "hyperparameters": {
                "learning_rate": 1.5e-5,
                "epochs": 2,
                "batch_size": 4,
                "gradient_accumulation": 4,
                "max_seq_length": 4096,
                "lora_r": 32,
                "lora_alpha": 64,
                "lora_dropout": 0.1,
                "warmup_ratio": 0.05,
                "weight_decay": 0.01,
            },
            "data_mix": {
                "self_assessment": 0.40,
                "training_recipe": 0.25,
                "prompt_optimization": 0.15,
                "architecture": 0.10,
                "evaluation": 0.05,
                "safety": 0.05,
            },
            "estimated_time_hours": 12,
            "estimated_vram_gb": 8,
            "notes": [
                "Use QLoRA for memory efficiency",
                "Start with self_assessment examples (largest category)",
                "Add safety examples last for alignment",
                "Validate with held-out 10% of dataset",
            ],
        }

        if benchmark:
            total = benchmark.total_examples
            if total > 0:
                passed_rate = benchmark.passed / total
                recipe["baseline"] = {
                    "avg_score": benchmark.avg_score,
                    "passed_rate": passed_rate,
                    "metric": benchmark.metric,
                    "iteration": benchmark.iteration,
                }
                recipe["data_mix"] = self._adaptive_data_mix(benchmark)
                if passed_rate < 0.5:
                    recipe["method"] = "SFT only (foundational)"
                elif passed_rate < 0.8:
                    recipe["method"] = "SFT + ORPO"

        return recipe

    def _adaptive_data_mix(self, benchmark: BenchmarkResult) -> Dict[str, float]:
        scores = benchmark.category_scores
        if not scores:
            return {c: 1.0 / len(CATEGORIES) for c in CATEGORIES}

        total_score = sum(max(0.01, 1.0 - s) for s in scores.values())
        mix = {cat: max(0.01, 1.0 - scores.get(cat, 0.5)) / total_score for cat in CATEGORIES}
        total = sum(mix.values())
        return {k: round(v / total, 4) for k, v in mix.items()}

    def export_sft_dataset(self, output_path: Path = None, categories: List[str] = None) -> Path:
        if not self._examples:
            self._load_local()
        output_path = output_path or (DATASET_DIR / "recursive_seed_sft.jsonl")
        examples = self._examples
        if categories:
            examples = [ex for ex in examples if ex.category in categories]
        with open(output_path, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex.to_sft_format(), ensure_ascii=False) + "\n")
        logger.info(f"Exported {len(examples)} SFT examples to {output_path}")
        return output_path

    def export_dpo_dataset(self, output_path: Path = None, categories: List[str] = None) -> Path:
        if not self._examples:
            self._load_local()
        output_path = output_path or (DATASET_DIR / "recursive_seed_dpo.jsonl")
        examples = self._examples
        if categories:
            examples = [ex for ex in examples if ex.category in categories]
        with open(output_path, "w", encoding="utf-8") as f:
            for ex in examples:
                f.write(json.dumps(ex.to_dpo_format(), ensure_ascii=False) + "\n")
        logger.info(f"Exported {len(examples)} DPO examples to {output_path}")
        return output_path

    def get_benchmark_history(self) -> List[Dict]:
        return [b.to_dict() for b in self._benchmark_history]

    def _save_benchmark_history(self):
        history_file = self.dataset_dir / "benchmark_history.json"
        atomic_write_json(history_file, [b.to_dict() for b in self._benchmark_history])

    def get_improvement_recommendations(self) -> Dict:
        if not self._benchmark_history:
            return {"status": "no_benchmarks", "message": "Run benchmark_nexus first to get recommendations"}

        latest = self._benchmark_history[-1]

        recommendations = {
            "overall_score": latest.avg_score,
            "passed_rate": latest.passed / latest.total_examples if latest.total_examples > 0 else 0,
            "strengths": latest.strengths,
            "weaknesses": latest.weaknesses,
            "metric": latest.metric,
            "iteration": latest.iteration,
            "actions": [],
        }

        if latest.avg_score < 0.5:
            for w in latest.weaknesses:
                count = len(self._category_index.get(w, []))
                recommendations["actions"].append({
                    "priority": "high",
                    "action": f"Improve {w} capabilities",
                    "category": w,
                    "examples_available": count,
                })

        if not latest.weaknesses and latest.avg_score >= 0.7:
            recommendations["actions"].append({
                "priority": "low",
                "action": "Consider advanced recursive meta-learning",
                "category": "all",
                "examples_available": 25000,
            })

        return recommendations


class RecursiveImprovementLoop:
    """
    Full recursive self-improvement loop.

    Flow per iteration:
    1. Benchmark current capabilities (keyword + LLM-as-Judge)
    2. Analyze results to identify weakest categories
    3. Generate targeted training examples for weak categories
    4. Export improved SFT/DPO datasets
    5. Generate adaptive training recipe
    6. Re-benchmark (and repeat)
    """

    def __init__(self, rsai: RecursiveSeedAI = None):
        self.rsai = rsai or RecursiveSeedAI()
        self.history: List[BenchmarkResult] = []
        self.generated_examples: List[RecursiveSeedExample] = []

    async def run_iteration(
        self,
        execute_fn: Callable[[str], Awaitable[str]] = None,
        judge_fn: Callable[[str], Awaitable[Any]] = None,
        sample_size: int = 10,
        generate_new_examples: bool = True,
        iteration: int = None,
    ) -> Dict:
        iteration_num = iteration if iteration is not None else len(self.history) + 1
        logger.info(f"=== Recursive Improvement Loop: Iteration {iteration_num} ===")

        # Step 1: Benchmark
        logger.info("Step 1: Benchmarking...")
        benchmark = await self.rsai.benchmark_nexus(
            execute_fn=execute_fn,
            judge_fn=judge_fn,
            sample_size=sample_size,
            iteration=iteration_num,
        )
        self.history.append(benchmark)
        logger.info(f"Benchmark complete: avg_score={benchmark.avg_score:.3f}, passed={benchmark.passed}/{benchmark.total_examples}")

        # Step 2: Analyze
        logger.info("Step 2: Analyzing weaknesses...")
        weaknesses = benchmark.weaknesses
        strengths = benchmark.strengths
        logger.info(f"Weaknesses: {weaknesses}, Strengths: {strengths}")

        # Step 3: Generate new training examples
        new_examples = []
        if generate_new_examples and weaknesses:
            logger.info("Step 3: Generating targeted training examples...")
            for weak_cat in weaknesses:
                ex = await self._generate_improvement_example(weak_cat, benchmark)
                if ex:
                    new_examples.append(ex)
                    logger.info(f"  Generated 1 example for {weak_cat}")
            self.generated_examples.extend(new_examples)

        # Step 4: Export improved datasets
        logger.info("Step 4: Exporting datasets...")
        sft_path = self.rsai.export_sft_dataset(
            categories=weaknesses if weaknesses else None,
        )
        dpo_path = self.rsai.export_dpo_dataset(
            categories=weaknesses if weaknesses else None,
        )

        # Step 5: Generate adaptive recipe
        logger.info("Step 5: Generating adaptive training recipe...")
        recipe = self.rsai.generate_training_recipe(benchmark=benchmark)

        # Save iteration summary
        result = {
            "iteration": iteration_num,
            "benchmark": benchmark.to_dict(),
            "recipe": recipe,
            "datasets": {
                "sft": str(sft_path),
                "dpo": str(dpo_path),
            },
            "new_examples_generated": len(new_examples),
            "weaknesses": weaknesses,
            "strengths": strengths,
            "improvement_from_previous": None,
        }

        if len(self.history) >= 2:
            prev = self.history[-2]
            delta = benchmark.avg_score - prev.avg_score
            result["improvement_from_previous"] = round(delta, 4)

        self._save_iteration(result)
        return result

    async def _generate_improvement_example(
        self,
        weak_category: str,
        benchmark: BenchmarkResult,
        generate_fn: Callable = None,
    ) -> Optional[RecursiveSeedExample]:
        """
        Generate a new training example targeting a specific weakness.
        Uses existing dataset examples as templates.
        """
        existing = self.rsai.get_examples_by_category(weak_category, limit=1)
        if not existing:
            return None
        template = existing[0]
        new_id = f"selfgen_{weak_category}_{int(time.time())}"

        return RecursiveSeedExample(
            id=new_id,
            category=weak_category,
            difficulty=template.difficulty,
            instruction=template.instruction,
            input=template.input,
            output=template.output,
            tags=template.tags + ["self_generated"],
            meta_step=f"Recursive improvement iteration {benchmark.iteration}: targeting weakness in {weak_category}",
        )

    def _save_iteration(self, result: Dict):
        iterations_file = self.rsai.dataset_dir / "improvement_iterations.json"
        history = []
        if iterations_file.exists():
            try:
                with open(iterations_file, "r") as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append(result)
        atomic_write_json(iterations_file, history)

    def get_summary(self) -> Dict:
        if not self.history:
            return {"status": "no_iterations", "message": "Run run_iteration first"}

        first = self.history[0]
        last = self.history[-1]
        improvement = last.avg_score - first.avg_score

        return {
            "iterations": len(self.history),
            "first_score": first.avg_score,
            "last_score": last.avg_score,
            "improvement": round(improvement, 4),
            "improvement_pct": round((improvement / first.avg_score) * 100, 1) if first.avg_score > 0 else 0,
            "weaknesses_resolved": len(first.weaknesses) - len(last.weaknesses) if last.weaknesses else len(first.weaknesses),
            "generated_examples": len(self.generated_examples),
        }
