"""
Decision Engine — Cerebro deterministico del Director.

Funciona SIN LLM. Pura logica Python:
- Decompose: pattern matching + templates -> Commands
- Assign: capability table -> best agent
- Evaluate: regex + assertions + exit code -> Verdict
- Prioritize: scoring formula
- Budget: allocation por dominio

Si hay LLM disponible, el Director puede usarlo para MEJORAR las decisiones,
pero el Decision Engine siempre tiene la ultima palabra.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable

from src.core.command_protocol import Command

logger = logging.getLogger(__name__)


@dataclass
class Verdict:
    passed: bool
    reason: str = ""
    confidence: float = 1.0


class CapabilityTable:
    """Tabla capability -> agent. Deterministic matching."""

    def __init__(self):
        self._agents: dict[str, list[str]] = {}

    def register(self, agent: str, capabilities: list[str]) -> None:
        self._agents[agent] = [c.lower() for c in capabilities]

    def unregister(self, agent: str) -> None:
        self._agents.pop(agent, None)

    def best_match(self, task: str) -> str:
        task_lower = task.lower()
        task_words = set(re.findall(r'\w+', task_lower))
        best_agent = "gema-director"
        best_score = 0
        for agent, caps in self._agents.items():
            score = sum(1 for cap in caps if cap in task_words or any(cap in w for w in task_words))
            if score > best_score:
                best_score = score
                best_agent = agent
        return best_agent

    def all_matches(self, task: str) -> list[tuple[str, int]]:
        task_lower = task.lower()
        task_words = set(re.findall(r'\w+', task_lower))
        matches = []
        for agent, caps in self._agents.items():
            score = sum(1 for cap in caps if cap in task_words or any(cap in w for w in task_words))
            if score > 0:
                matches.append((agent, score))
        return sorted(matches, key=lambda x: -x[1])

    @property
    def agents(self) -> list[str]:
        return list(self._agents.keys())


class PriorityScorer:
    """Scoring de prioridad basado en keywords."""

    URGENT_KEYWORDS = {"fix", "bug", "critical", "crash", "broken", "urgent", "hotfix", "security", "vulnerability"}
    HIGH_KEYWORDS = {"implement", "add", "create", "build", "new", "feature"}
    LOW_KEYWORDS = {"refactor", "clean", "organize", "docs", "comment", "readme", "style"}

    def score(self, task: str) -> int:
        """1=critical, 5=low."""
        words = set(re.findall(r'\w+', task.lower()))
        if words & self.URGENT_KEYWORDS:
            return 1
        if words & self.HIGH_KEYWORDS:
            return 2
        if words & self.LOW_KEYWORDS:
            return 4
        return 3


# --- Task decomposition templates ---

DECOMPOSITION_PATTERNS: list[tuple[re.Pattern, list[dict]]] = [
    # "implementar X + tests"
    (re.compile(r'(?:implementar?|crear?|build|add)\b.*(?:y\b|and\b|\+)\s*(?:test|prueba)', re.I), [
        {"action": "execute", "suffix": " (implementacion)", "domain": "code"},
        {"action": "execute", "suffix": " (tests)", "domain": "test"},
    ]),
    # "implementar X + documentacion"
    (re.compile(r'(?:implementar?|crear?|build)\b.*(?:y\b|and\b|\+)\s*(?:doc|documentacion)', re.I), [
        {"action": "execute", "suffix": " (implementacion)", "domain": "code"},
        {"action": "execute", "suffix": " (documentacion)", "domain": "docs"},
    ]),
    # "analizar/investigar X"
    (re.compile(r'(?:analizar?|investigar?|research|buscar?|estudiar?)', re.I), [
        {"action": "analyze", "suffix": "", "domain": "research"},
    ]),
    # "refactorizar X"
    (re.compile(r'(?:refactor|limpiar?|clean|reorganizar?)', re.I), [
        {"action": "execute", "suffix": " (refactor)", "domain": "code"},
        {"action": "execute", "suffix": " (verificar tests post-refactor)", "domain": "test"},
    ]),
    # "fix/arreglar bug"
    (re.compile(r'(?:fix|arreglar?|reparar?|bug|error)', re.I), [
        {"action": "analyze", "suffix": " (diagnostico)", "domain": "debug"},
        {"action": "execute", "suffix": " (fix)", "domain": "code"},
    ]),
    # "deploy/desplegar"
    (re.compile(r'(?:deploy|desplegar?|release|ship)', re.I), [
        {"action": "execute", "suffix": " (pre-deploy checks)", "domain": "ops"},
        {"action": "execute", "suffix": " (deploy)", "domain": "ops"},
    ]),
]

DOMAIN_CAPABILITIES = {
    "code": ["code", "implement", "refactor", "build"],
    "test": ["test", "qa", "validate", "verify"],
    "debug": ["debug", "diagnose", "fix", "troubleshoot"],
    "research": ["research", "search", "analyze", "investigate"],
    "docs": ["docs", "document", "readme", "write"],
    "ops": ["deploy", "devops", "docker", "ci", "cd"],
}

BUDGET_BY_DOMAIN = {
    "code": {"small": 3000, "medium": 8000, "large": 15000},
    "test": {"small": 2000, "medium": 5000, "large": 10000},
    "debug": {"small": 2000, "medium": 6000, "large": 12000},
    "research": {"small": 3000, "medium": 8000, "large": 20000},
    "docs": {"small": 1000, "medium": 3000, "large": 8000},
    "ops": {"small": 2000, "medium": 5000, "large": 10000},
}


class DecisionEngine:
    """
    Cerebro deterministico del Director NEXUS.
    Funciona al 100% sin LLM.
    """

    def __init__(self):
        self.capabilities = CapabilityTable()
        self.priority_scorer = PriorityScorer()
        self._decomposition_count = 0

    def decompose(self, task: str) -> list[Command]:
        """Descompone tarea en Commands usando pattern matching."""
        self._decomposition_count += 1
        priority = self.priority_scorer.score(task)

        # Try pattern-based decomposition
        for pattern, templates in DECOMPOSITION_PATTERNS:
            if pattern.search(task):
                commands = []
                for tmpl in templates:
                    target = self._assign_by_domain(tmpl["domain"], task)
                    commands.append(Command(
                        target=target,
                        action=tmpl["action"],
                        instruction={"task": task + tmpl["suffix"]},
                        priority=priority,
                        deadline_tokens=self.budget_allocate(tmpl["domain"], "medium"),
                    ))
                return commands

        # Fallback: single command assigned to best match
        target = self.capabilities.best_match(task)
        return [Command(
            target=target,
            action="execute",
            instruction={"task": task},
            priority=priority,
            deadline_tokens=self.budget_allocate("code", "medium"),
        )]

    def assign(self, task: str) -> str:
        """Asigna la tarea al mejor agente. Deterministico."""
        return self.capabilities.best_match(task)

    def evaluate(self, output: str = "", exit_code: int = 0, error: str | None = None) -> Verdict:
        """Evalua resultado SIN LLM: exit code + regex + assertions."""
        if exit_code != 0:
            return Verdict(passed=False, reason=error or f"Exit code {exit_code}", confidence=1.0)
        if error:
            return Verdict(passed=False, reason=error, confidence=1.0)
        if not output or not output.strip():
            return Verdict(passed=False, reason="Empty output", confidence=0.8)

        # Check for common error patterns in output
        error_patterns = [
            r'(?:Error|Exception|Traceback|FAILED|FATAL)',
            r'(?:SyntaxError|TypeError|ValueError|ImportError)',
            r'(?:panic|segfault|core dump)',
        ]
        for pat in error_patterns:
            match = re.search(pat, output, re.I)
            if match:
                return Verdict(passed=False, reason=f"Error pattern found: {match.group()}", confidence=0.9)

        return Verdict(passed=True, reason="OK", confidence=0.9)

    def prioritize(self, tasks: list[str]) -> list[tuple[str, int]]:
        """Ordena tareas por prioridad (1=mas urgente)."""
        scored = [(t, self.priority_scorer.score(t)) for t in tasks]
        return sorted(scored, key=lambda x: x[1])

    def budget_allocate(self, domain: str = "code", task_size: str = "medium") -> int:
        """Asigna token budget por dominio y tamano."""
        budgets = BUDGET_BY_DOMAIN.get(domain, BUDGET_BY_DOMAIN["code"])
        return budgets.get(task_size, budgets["medium"])

    def _assign_by_domain(self, domain: str, task: str) -> str:
        """Asigna agente por dominio, luego por capability match."""
        domain_caps = DOMAIN_CAPABILITIES.get(domain, [])
        # Find agent with matching domain capabilities
        for agent in self.capabilities.agents:
            agent_caps = self.capabilities._agents.get(agent, [])
            if any(dc in agent_caps for dc in domain_caps):
                return agent
        # Fallback to best general match
        return self.capabilities.best_match(task)

    def status(self) -> dict:
        return {
            "decomposition_count": self._decomposition_count,
            "registered_agents": self.capabilities.agents,
            "available_domains": list(DOMAIN_CAPABILITIES.keys()),
        }


class LLMAdapter:
    """
    Adapter opcional que MEJORA las decisiones del DecisionEngine usando un LLM.
    Si el LLM no está disponible, el DecisionEngine funciona igual sin él.
    El LLM es herramienta, no cerebro.
    """

    def __init__(self, llm_call: Callable[..., Any] | None = None):
        self._llm_call = llm_call
        self._available = llm_call is not None

    @property
    def available(self) -> bool:
        return self._available

    async def enhance_decomposition(self, task: str, commands: list[Command]) -> list[Command]:
        """Usa LLM para refinar la descomposición del DecisionEngine."""
        if not self._available:
            return commands
        try:
            prompt = (
                f"Task: {task}\n"
                f"Current decomposition ({len(commands)} commands):\n"
                + "\n".join(f"  - {c.target}: {c.action} — {c.instruction.get('task','')[:80]}" for c in commands)
                + "\n\nRefine: suggest better targets, missing steps, or confirm this is correct. "
                "Respond JSON: {\"refined\": true/false, \"suggestions\": [str]}"
            )
            response = await self._llm_call(prompt)
            # Parse suggestions but don't override — DecisionEngine has final say
            if isinstance(response, str) and "refined" in response:
                logger.info(f"LLMAdapter: enhancement received for {len(commands)} commands")
            return commands  # DecisionEngine decides, LLM only advises
        except Exception as e:
            logger.warning(f"LLMAdapter.enhance_decomposition failed: {e}")
            return commands

    async def synthesize(self, results: list[dict], task: str) -> str:
        """Usa LLM para sintetizar resultados de múltiples commands en un resumen."""
        if not self._available:
            return f"Completed {len(results)} subtasks for: {task[:80]}"
        try:
            prompt = (
                f"Synthesize results for task: {task}\n"
                f"Results ({len(results)}):\n"
                + "\n".join(f"  - {r.get('status','?')}: {r.get('output','')[:100]}" for r in results)
                + "\n\nProvide a concise summary (1-3 sentences)."
            )
            response = await self._llm_call(prompt)
            return str(response)[:500] if response else f"Completed {len(results)} subtasks"
        except Exception as e:
            logger.warning(f"LLMAdapter.synthesize failed: {e}")
            return f"Completed {len(results)} subtasks for: {task[:80]}"

    async def judge(self, output: str, task: str, verdict: "Verdict") -> "Verdict":
        """LLM second opinion on DecisionEngine's verdict. Engine has final say."""
        if not self._available:
            return verdict
        try:
            prompt = (
                f"Task: {task}\nOutput: {output[:500]}\n"
                f"Deterministic verdict: passed={verdict.passed}, reason={verdict.reason}\n"
                "Do you agree? Respond JSON: {\"agree\": true/false, \"reason\": \"...\"}"
            )
            response = await self._llm_call(prompt)
            # LLM opinion logged but DecisionEngine verdict prevails
            if response:
                logger.info(f"LLMAdapter.judge opinion received (engine verdict: {verdict.passed})")
            return verdict  # Engine always has final say
        except Exception as e:
            logger.warning(f"LLMAdapter.judge failed: {e}")
            return verdict
