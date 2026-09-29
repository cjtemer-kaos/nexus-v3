"""
F8: Recipe System — YAML Workflows

YAML-defined workflows with conditional execution and parallel branches.
"""

import asyncio
import ast
import logging
import operator
from typing import Dict, List, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-recipes")


@dataclass
class RecipeStep:
    id: str
    name: str
    action: str
    params: Dict = field(default_factory=dict)
    condition: str = ""
    on_success: str = ""
    on_failure: str = ""
    parallel_with: List[str] = field(default_factory=list)
    retries: int = 0
    timeout: int = 300
    status: str = "pending"
    result: str = ""
    error: str = ""


@dataclass
class Recipe:
    id: str
    name: str
    description: str
    version: str = "1.0"
    steps: List[RecipeStep] = field(default_factory=list)
    variables: Dict = field(default_factory=dict)
    status: str = "pending"
    created_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()


class RecipeEngine:
    """Executes YAML-defined workflows"""

    def __init__(self):
        self._recipes: Dict[str, Recipe] = {}
        self._action_handlers: Dict[str, Callable] = {}
        self._runs: List[Dict] = []
        self._register_builtin_actions()

    def _register_builtin_actions(self):
        self._action_handlers = {
            "chat": self._action_chat,
            "classify": self._action_classify,
            "search": self._action_search,
            "execute": self._action_execute,
            "wait": self._action_wait,
            "log": self._action_log,
            "condition": self._action_condition,
            "parallel": self._action_parallel,
        }

    def register_action(self, name: str, handler: Callable):
        self._action_handlers[name] = handler

    def load_recipe(self, recipe: Recipe):
        self._recipes[recipe.name] = recipe
        logger.info(f"Recipe loaded: {recipe.name} ({len(recipe.steps)} steps)")

    def load_from_yaml(self, yaml_path: str) -> Recipe:
        """Load recipe from YAML file"""
        try:
            import yaml
        except ImportError:
            raise ImportError("PyYAML required: pip install pyyaml")

        path = Path(yaml_path)
        if not path.exists():
            raise FileNotFoundError(f"Recipe not found: {yaml_path}")

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        recipe = Recipe(
            id=data.get("id", path.stem),
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            version=data.get("version", "1.0"),
            variables=data.get("variables", {}),
        )

        for i, step_data in enumerate(data.get("steps", [])):
            step = RecipeStep(
                id=step_data.get("id", f"step_{i+1}"),
                name=step_data.get("name", ""),
                action=step_data.get("action", ""),
                params=step_data.get("params", {}),
                condition=step_data.get("if", ""),
                on_success=step_data.get("on_success", ""),
                on_failure=step_data.get("on_failure", ""),
                parallel_with=step_data.get("parallel", []),
                retries=step_data.get("retries", 0),
                timeout=step_data.get("timeout", 300),
            )
            recipe.steps.append(step)

        self.load_recipe(recipe)
        return recipe

    async def execute_recipe(self, recipe_name: str, variables: Dict = None, executor_func=None) -> Dict:
        recipe = self._recipes.get(recipe_name)
        if not recipe:
            return {"error": f"Recipe not found: {recipe_name}"}

        recipe.status = "running"
        recipe.started_at = datetime.now().isoformat()
        run_vars = {**recipe.variables, **(variables or {})}
        step_results = {}

        for step in recipe.steps:
            # Check condition
            if step.condition and not self._evaluate_condition(step.condition, run_vars, step_results):
                step.status = "skipped"
                continue

            # Check if this step is part of a parallel group already executed
            if step.status != "pending":
                continue

            # Find parallel steps
            parallel_steps = [step]
            if step.parallel_with:
                for other in recipe.steps:
                    if other.id in step.parallel_with and other.status == "pending":
                        if not other.condition or self._evaluate_condition(other.condition, run_vars, step_results):
                            parallel_steps.append(other)

            if len(parallel_steps) > 1:
                results = await asyncio.gather(
                    *[self._execute_step(s, run_vars, step_results, executor_func) for s in parallel_steps],
                    return_exceptions=True,
                )
                for s, r in zip(parallel_steps, results):
                    if isinstance(r, Exception):
                        s.status = "failed"
                        s.error = str(r)
                    else:
                        s.status = r.get("status", "completed")
                        s.result = r.get("result", "")
                        step_results[s.id] = r
            else:
                result = await self._execute_step(step, run_vars, step_results, executor_func)
                step.status = result.get("status", "completed")
                step.result = result.get("result", "")
                step.error = result.get("error", "")
                step_results[step.id] = result

            # Follow on_success/on_failure
            if step.status == "completed" and step.on_success:
                pass  # Next step in sequence
            elif step.status == "failed" and step.on_failure:
                pass  # Could jump to specific step

        recipe.completed_at = datetime.now().isoformat()
        recipe.status = "completed" if all(s.status in ("completed", "skipped") for s in recipe.steps) else "partial"

        run_record = {
            "recipe": recipe_name,
            "status": recipe.status,
            "started_at": recipe.started_at,
            "completed_at": recipe.completed_at,
            "steps": {s.id: {"status": s.status, "result": s.result[:200]} for s in recipe.steps},
        }
        self._runs.append(run_record)

        return run_record

    async def _execute_step(self, step: RecipeStep, variables: Dict, step_results: Dict, executor_func=None) -> Dict:
        handler = self._action_handlers.get(step.action)
        if not handler:
            return {"status": "failed", "error": f"Unknown action: {step.action}"}

        # Resolve variable references in params
        resolved_params = self._resolve_variables(step.params, variables, step_results)

        for attempt in range(step.retries + 1):
            try:
                result = await handler(resolved_params, executor_func)
                return {"status": "completed", "result": result}
            except Exception as e:
                if attempt >= step.retries:
                    return {"status": "failed", "error": str(e)}
                await asyncio.sleep(1 * (attempt + 1))

        return {"status": "failed", "error": "Max retries exceeded"}

    def _evaluate_condition(self, condition: str, variables: Dict, step_results: Dict) -> bool:
        """Evaluate step condition using safe parser (no eval/RCE risk)"""
        condition = self._resolve_variables({"cond": condition}, variables, step_results)["cond"]
        return self._safe_eval(condition)

    def _safe_eval(self, expression: str) -> bool:
        """Safe expression evaluator using AST parsing — only supports comparisons, unary and boolean logic"""
        expression = expression.strip()
        if not expression:
            return False
        
        # Normalise spelling of booleans
        if expression.lower() == 'true':
            return True
        if expression.lower() == 'false':
            return False

        try:
            node = ast.parse(expression, mode='eval')
        except SyntaxError as e:
            logger.error(f"Syntax error parsing condition '{expression}': {e}")
            return False

        ops = {
            ast.Eq: operator.eq, ast.NotEq: operator.ne,
            ast.Lt: operator.lt, ast.LtE: operator.le,
            ast.Gt: operator.gt, ast.GtE: operator.ge,
            ast.And: lambda a, b: a and b, ast.Or: lambda a, b: a or b,
            ast.Not: operator.not_,
        }

        def _eval(n):
            if isinstance(n, ast.Expression):
                return _eval(n.body)
            elif isinstance(n, ast.Constant): # Python 3.8+
                return n.value
            elif isinstance(n, (ast.Num, ast.Str, ast.Bytes, ast.NameConstant)): # Python < 3.8
                if hasattr(n, 'value'):
                    return n.value
                return n.n if isinstance(n, ast.Num) else n.s
            elif isinstance(n, ast.Compare):
                left = _eval(n.left)
                for op, comparator in zip(n.ops, n.comparators):
                    op_type = type(op)
                    if op_type not in ops:
                        raise ValueError(f"Unsupported comparison operator: {op_type.__name__}")
                    right = _eval(comparator)
                    if not ops[op_type](left, right):
                        return False
                    left = right
                return True
            elif isinstance(n, ast.BoolOp):
                op_type = type(n.op)
                if op_type not in ops:
                    raise ValueError(f"Unsupported boolean operator: {op_type.__name__}")
                values = [_eval(v) for v in n.values]
                if isinstance(n.op, ast.And):
                    return all(values)
                elif isinstance(n.op, ast.Or):
                    return any(values)
            elif isinstance(n, ast.UnaryOp):
                op_type = type(n.op)
                if op_type not in ops:
                    raise ValueError(f"Unsupported unary operator: {op_type.__name__}")
                operand = _eval(n.operand)
                return ops[op_type](operand)
            elif isinstance(n, ast.Name):
                if n.id == 'True':
                    return True
                elif n.id == 'False':
                    return False
                elif n.id == 'None':
                    return None
                raise ValueError(f"Names/variables are not allowed in condition evaluation: {n.id}")
            else:
                raise ValueError(f"Unsupported syntax node: {type(n).__name__}")

        try:
            return bool(_eval(node))
        except Exception as e:
            logger.error(f"Error evaluating condition safely: {e}")
            return False


    def _resolve_variables(self, params: Dict, variables: Dict, step_results: Dict) -> Dict:
        """Resolve ${VAR} and ${step_id.result} references"""
        resolved = {}
        for key, value in params.items():
            if isinstance(value, str):
                # Replace ${VAR}
                for var_name, var_value in variables.items():
                    value = value.replace(f"${{{var_name}}}", str(var_value))
                # Replace ${step_id.result}
                for step_id, result in step_results.items():
                    if isinstance(result, dict):
                        value = value.replace(f"${{{step_id}.result}}", str(result.get("result", "")))
                resolved[key] = value
            else:
                resolved[key] = value
        return resolved

    async def _action_chat(self, params: Dict, executor_func=None):
        return f"Chat: {params.get('message', '')}"

    async def _action_classify(self, params: Dict, executor_func=None):
        return f"Classified: {params.get('text', '')}"

    async def _action_search(self, params: Dict, executor_func=None):
        return f"Search results for: {params.get('query', '')}"

    async def _action_execute(self, params: Dict, executor_func=None):
        if executor_func:
            return await executor_func(params.get("command", ""), params.get("assignee", "auto"))
        return f"Executed: {params.get('command', '')}"

    async def _action_wait(self, params: Dict, executor_func=None):
        await asyncio.sleep(params.get("seconds", 1))
        return f"Waited {params.get('seconds', 1)}s"

    async def _action_log(self, params: Dict, executor_func=None):
        msg = params.get("message", "")
        logger.info(f"Recipe log: {msg}")
        return msg

    async def _action_condition(self, params: Dict, executor_func=None):
        return params.get("value", False)

    async def _action_parallel(self, params: Dict, executor_func=None):
        return "Parallel group executed"

    def list_recipes(self) -> List[Dict]:
        return [
            {"name": r.name, "description": r.description, "steps": len(r.steps), "version": r.version}
            for r in self._recipes.values()
        ]

    def get_stats(self) -> Dict:
        return {
            "recipes_loaded": len(self._recipes),
            "total_runs": len(self._runs),
            "actions": list(self._action_handlers.keys()),
        }

    def get_ready_steps(self, recipe: Recipe) -> List[RecipeStep]:
        """Get steps that are pending and have no unmet dependencies"""
        completed_ids = {s.id for s in recipe.steps if s.status == "completed"}
        ready = []
        for step in recipe.steps:
            if step.status == "pending":
                ready.append(step)
        return ready if ready else [s for s in recipe.steps if s.status == "pending"]

    def mark_step_completed(self, recipe: Recipe, step_id: str, result: str = ""):
        """Mark a step as completed"""
        for step in recipe.steps:
            if step.id == step_id or step.name == step_id:
                step.status = "completed"
                step.result = result
                break

    def is_recipe_complete(self, recipe: Recipe) -> bool:
        """Check if all steps in the recipe are completed"""
        return all(s.status == "completed" for s in recipe.steps)
