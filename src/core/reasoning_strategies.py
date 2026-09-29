"""
ReasoningStrategies - Estrategias de razonamiento para SuperNEXUS v2.0

Implementa:
- Chain-of-Thought (CoT)
- Tree-of-Thought (ToT)
- Debate entre gemas
Basado en el patrón #17 del curso de Google: "Reasoning Strategies"
"""

import logging
import asyncio
from typing import Dict, List, Callable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ReasoningType(Enum):
    CHAIN_OF_THOUGHT = "chain_of_thought"
    TREE_OF_THOUGHT = "tree_of_thought"
    DEBATE = "debate"


@dataclass
class ReasoningResult:
    """Resultado de razonamiento"""
    reasoning_type: ReasoningType
    answer: str
    steps: List[str]
    confidence: float
    metadata: Dict = field(default_factory=dict)


class ChainOfThought:
    """
    Chain-of-Thought: descomponer problema en pasos secuenciales.
    """
    
    def __init__(self, generate_func: Callable = None):
        self.generate_func = generate_func
    
    async def reason(self, task: str, context: str = "") -> ReasoningResult:
        """Razona paso a paso"""
        steps_prompt = (
            f"Task: {task}\n\n"
            "Think step by step:\n"
            "1. Understand the problem\n"
            "2. Break it into sub-problems\n"
            "3. Solve each sub-problem\n"
            "4. Combine solutions\n"
            "5. Verify the result\n\n"
            "Show your reasoning for each step."
        )
        
        if self.generate_func:
            output = await self.generate_func(steps_prompt, context)
        else:
            output = "Chain-of-Thought requires a generate function."
        
        steps = self._extract_steps(output)
        
        return ReasoningResult(
            reasoning_type=ReasoningType.CHAIN_OF_THOUGHT,
            answer=output,
            steps=steps,
            confidence=0.7,
            metadata={"steps_count": len(steps)},
        )
    
    def _extract_steps(self, output: str) -> List[str]:
        """Extrae pasos del output"""
        steps = []
        for line in output.split("\n"):
            line = line.strip()
            if any(line.startswith(f"{i}.") or line.startswith(f"Step {i}") for i in range(1, 10)):
                steps.append(line)
        return steps or [output]


class TreeOfThought:
    """
    Tree-of-Thought: explorar múltiples caminos de razonamiento.
    """
    
    def __init__(self, generate_func: Callable = None, branches: int = 3):
        self.generate_func = generate_func
        self.branches = branches
    
    async def reason(self, task: str, context: str = "") -> ReasoningResult:
        """Explora múltiples caminos"""
        branch_prompts = []
        
        for i in range(self.branches):
            approach = [
                "Approach this analytically: break down facts and logic.",
                "Approach this creatively: think outside the box.",
                "Approach this practically: focus on actionable solutions.",
                "Approach this systematically: consider all edge cases.",
                "Approach this from first principles: start from basics.",
            ]
            
            branch_prompts.append(
                f"Task: {task}\n\n"
                f"{approach[i % len(approach)]}\n\n"
                "Provide your reasoning and conclusion."
            )
        
        if self.generate_func:
            tasks = [self.generate_func(p, context) for p in branch_prompts]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            outputs = [r if isinstance(r, str) else str(r) for r in results]
        else:
            outputs = ["Tree-of-Thought requires a generate function."] * self.branches
        
        best_output = max(outputs, key=len)
        
        return ReasoningResult(
            reasoning_type=ReasoningType.TREE_OF_THOUGHT,
            answer=best_output,
            steps=[f"Branch {i+1}: {o[:100]}..." for i, o in enumerate(outputs)],
            confidence=0.75,
            metadata={"branches": self.branches, "outputs_count": len(outputs)},
        )


class DebatePattern:
    """
    Debate: múltiples agentes discuten para llegar a mejor respuesta.
    """
    
    def __init__(self, generate_func: Callable = None, rounds: int = 2):
        self.generate_func = generate_func
        self.rounds = rounds
    
    async def debate(
        self,
        task: str,
        agents: List[Dict] = None,
        context: str = "",
    ) -> ReasoningResult:
        """Ejecuta debate entre agentes"""
        if agents is None:
            agents = [
                {"name": "Proponent", "role": "Argue in favor of the best solution."},
                {"name": "Critic", "role": "Find flaws and suggest improvements."},
                {"name": "Moderator", "role": "Synthesize and reach conclusion."},
            ]
        
        history = []
        
        for round_num in range(self.rounds):
            round_responses = []
            
            for agent in agents:
                prompt = (
                    f"Task: {task}\n\n"
                    f"You are {agent['name']}. {agent['role']}\n\n"
                )
                
                if history:
                    prompt += f"Previous discussion:\n{''.join(history)}\n\n"
                
                prompt += "Provide your response:"
                
                if self.generate_func:
                    response = await self.generate_func(prompt, context)
                else:
                    response = f"[{agent['name']}]: Debate requires generate function."
                
                round_responses.append(f"[{agent['name']}]: {response}")
            
            history.extend(round_responses)
        
        conclusion = history[-1] if history else "No conclusion reached."
        
        return ReasoningResult(
            reasoning_type=ReasoningType.DEBATE,
            answer=conclusion,
            steps=history,
            confidence=0.8,
            metadata={"rounds": self.rounds, "agents": len(agents)},
        )


class ReasoningStrategies:
    """
    Gestor unificado de estrategias de razonamiento.
    
    Uso:
        reasoning = ReasoningStrategies(generate_func=my_llm_call)
        result = await reasoning.reason(task, strategy="debate")
    """
    
    def __init__(self, generate_func: Callable = None):
        self.generate_func = generate_func
        self.cot = ChainOfThought(generate_func)
        self.tot = TreeOfThought(generate_func)
        self.debate = DebatePattern(generate_func)
    
    async def reason(
        self,
        task: str,
        strategy: str = "auto",
        context: str = "",
    ) -> ReasoningResult:
        """Razona usando estrategia especificada"""
        if strategy == "auto":
            strategy = self._select_strategy(task)
        
        if strategy == "chain_of_thought" or strategy == "cot":
            return await self.cot.reason(task, context)
        elif strategy == "tree_of_thought" or strategy == "tot":
            return await self.tot.reason(task, context)
        elif strategy == "debate":
            return await self.debate.debate(task, context=context)
        else:
            return await self.cot.reason(task, context)
    
    def _select_strategy(self, task: str) -> str:
        """Selecciona estrategia automáticamente"""
        task_lower = task.lower()
        
        if any(kw in task_lower for kw in ["debug", "error", "fix", "bug"]):
            return "chain_of_thought"
        elif any(kw in task_lower for kw in ["design", "architect", "plan", "compare"]):
            return "tree_of_thought"
        elif any(kw in task_lower for kw in ["decide", "choose", "evaluate", "opinion"]):
            return "debate"
        else:
            return "chain_of_thought"
    
    def get_status(self) -> Dict:
        return {
            "strategies": ["chain_of_thought", "tree_of_thought", "debate"],
            "default": "auto",
        }
