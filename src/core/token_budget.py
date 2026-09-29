"""
Token Budget Enforcement — F5

Hard cap on token spend per team run with alerts.
Applies globally and per-agent with individual thresholds
(pattern extracted from openclaw budget-governance.ts).
"""

import logging
import time
from typing import Dict, Optional, Callable, List
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("nexus-budget")

DEFAULT_WARN_RATIO = 0.8


@dataclass
class AgentBudgetConfig:
    agent_id: str
    max_tokens_per_run: int = 25000
    max_tokens_per_hour: int = 100000
    warn_ratio: float = DEFAULT_WARN_RATIO
    hard_cap: bool = True
    label: str = ""


@dataclass
class BudgetConfig:
    max_tokens_per_run: int = 500000
    max_tokens_per_message: int = 8000
    max_tokens_per_hour: int = 1000000
    alert_threshold: float = 0.8
    hard_cap: bool = True


@dataclass
class AgentBudgetState:
    agent_id: str
    tokens_this_run: int = 0
    tokens_this_hour: int = 0
    alerts_sent: List[str] = field(default_factory=list)
    hard_cap_triggered: bool = False


@dataclass
class BudgetState:
    run_id: str = ""
    tokens_this_run: int = 0
    tokens_this_hour: int = 0
    messages_this_run: int = 0
    run_start: float = 0.0
    hour_start: float = 0.0
    alerts_sent: List[str] = field(default_factory=list)
    hard_cap_triggered: bool = False
    agents: Dict[str, AgentBudgetState] = field(default_factory=dict)
    agent_configs: Dict[str, AgentBudgetConfig] = field(default_factory=dict)

    def __post_init__(self):
        now = time.time()
        if not self.run_start:
            self.run_start = now
        if not self.hour_start:
            self.hour_start = now


class TokenBudget:
    """Enforces token budget across the entire system"""

    def __init__(self, config: Optional[BudgetConfig] = None):
        self.config = config or BudgetConfig()
        self.state = BudgetState(run_id=datetime.now().strftime("%Y%m%d_%H%M%S"))
        self._callbacks: List[Callable] = []

    def register_agent(self, agent_id: str, config: Optional[AgentBudgetConfig] = None) -> AgentBudgetConfig:
        if agent_id not in self.state.agents:
            cfg = config or AgentBudgetConfig(agent_id=agent_id)
            self.state.agent_configs[agent_id] = cfg
            self.state.agents[agent_id] = AgentBudgetState(agent_id=agent_id)
        return self.state.agent_configs[agent_id]

    def get_agent_status(self, agent_id: str) -> Optional[Dict]:
        state = self.state.agents.get(agent_id)
        config = self.state.agent_configs.get(agent_id)
        if not state or not config:
            return None
        run_pct = (state.tokens_this_run / max(config.max_tokens_per_run, 1)) * 100
        hour_pct = (state.tokens_this_hour / max(config.max_tokens_per_hour, 1)) * 100
        return {
            "agent_id": agent_id,
            "tokens_this_run": state.tokens_this_run,
            "max_tokens_per_run": config.max_tokens_per_run,
            "run_percent": round(run_pct, 1),
            "tokens_this_hour": state.tokens_this_hour,
            "max_tokens_per_hour": config.max_tokens_per_hour,
            "hour_percent": round(hour_pct, 1),
            "hard_cap_triggered": state.hard_cap_triggered,
        }

    def get_all_agent_statuses(self) -> Dict[str, Dict]:
        return {
            agent_id: self.get_agent_status(agent_id)
            for agent_id in self.state.agents
        }

    def _check_agent_budget(self, agent_id: str, tokens: int) -> Dict:
        state = self.state.agents.get(agent_id)
        config = self.state.agent_configs.get(agent_id)
        if not state or not config:
            return {"allowed": True}

        state.tokens_this_run += tokens
        state.tokens_this_hour += tokens
        run_pct = state.tokens_this_run / config.max_tokens_per_run
        hour_pct = state.tokens_this_hour / config.max_tokens_per_hour

        if run_pct >= config.warn_ratio and f"{agent_id}_run_warn" not in state.alerts_sent:
            state.alerts_sent.append(f"{agent_id}_run_warn")
            self._send_alert(f"{agent_id}_run_warn", f"Agent {agent_id} at {run_pct*100:.0f}% of budget")

        if hour_pct >= config.warn_ratio and f"{agent_id}_hour_warn" not in state.alerts_sent:
            state.alerts_sent.append(f"{agent_id}_hour_warn")
            self._send_alert(f"{agent_id}_hour_warn", f"Agent {agent_id} hour at {hour_pct*100:.0f}%")

        if config.hard_cap and (state.tokens_this_run >= config.max_tokens_per_run or state.tokens_this_hour >= config.max_tokens_per_hour):
            state.hard_cap_triggered = True
            return {"allowed": False, "reason": f"agent_{agent_id}_cap_reached"}

        return {"allowed": True}

    def register_alert_callback(self, callback: Callable):
        self._callbacks.append(callback)

    def _send_alert(self, alert_type: str, message: str):
        if alert_type not in self.state.alerts_sent:
            self.state.alerts_sent.append(alert_type)
            logger.warning(f"TOKEN BUDGET ALERT [{alert_type}]: {message}")
            for cb in self._callbacks:
                try:
                    cb(alert_type, message)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")

    def _check_hour_reset(self):
        if time.time() - self.state.hour_start > 3600:
            self.state.tokens_this_hour = 0
            self.state.hour_start = time.time()

    def _check_run_reset(self):
        pass

    def record_tokens(self, tokens: int, source: str = "unknown", agent_id: str = "") -> Dict:
        """Record token usage and check budget"""
        self._check_hour_reset()

        self.state.tokens_this_run += tokens
        self.state.tokens_this_hour += tokens
        self.state.messages_this_run += 1

        if agent_id and agent_id in self.state.agents:
            agent_result = self._check_agent_budget(agent_id, tokens)
            if not agent_result["allowed"]:
                return agent_result

        run_pct = self.state.tokens_this_run / self.config.max_tokens_per_run
        hour_pct = self.state.tokens_this_hour / self.config.max_tokens_per_hour

        # Alert thresholds
        if run_pct >= self.config.alert_threshold and "run_80" not in self.state.alerts_sent:
            self._send_alert("run_80", f"Run at {run_pct*100:.0f}% of budget ({self.state.tokens_this_run}/{self.config.max_tokens_per_run})")

        if hour_pct >= self.config.alert_threshold and "hour_80" not in self.state.alerts_sent:
            self._send_alert("hour_80", f"Hour at {hour_pct*100:.0f}% of budget ({self.state.tokens_this_hour}/{self.config.max_tokens_per_hour})")

        # Hard cap enforcement
        if self.config.hard_cap:
            if self.state.tokens_this_run >= self.config.max_tokens_per_run:
                self.state.hard_cap_triggered = True
                self._send_alert("run_cap", f"Run token cap reached ({self.state.tokens_this_run})")
                return {
                    "allowed": False,
                    "reason": "run_token_cap_exceeded",
                    "tokens_used": self.state.tokens_this_run,
                    "limit": self.config.max_tokens_per_run,
                }

            if self.state.tokens_this_hour >= self.config.max_tokens_per_hour:
                self._send_alert("hour_cap", f"Hourly token cap reached ({self.state.tokens_this_hour})")
                return {
                    "allowed": False,
                    "reason": "hourly_token_cap_exceeded",
                    "tokens_used": self.state.tokens_this_hour,
                    "limit": self.config.max_tokens_per_hour,
                }

            if tokens > self.config.max_tokens_per_message:
                return {
                    "allowed": False,
                    "reason": "message_token_limit_exceeded",
                    "tokens": tokens,
                    "limit": self.config.max_tokens_per_message,
                }

        return {
            "allowed": True,
            "tokens_recorded": tokens,
            "run_total": self.state.tokens_this_run,
            "run_remaining": self.config.max_tokens_per_run - self.state.tokens_this_run,
            "run_percent": round(run_pct * 100, 1),
            "hour_total": self.state.tokens_this_hour,
            "hour_remaining": self.config.max_tokens_per_hour - self.state.tokens_this_hour,
        }

    def is_within_budget(self) -> bool:
        return not self.state.hard_cap_triggered

    def get_status(self) -> Dict:
        run_pct = (self.state.tokens_this_run / max(self.config.max_tokens_per_run, 1)) * 100
        hour_pct = (self.state.tokens_this_hour / max(self.config.max_tokens_per_hour, 1)) * 100
        return {
            "run_id": self.state.run_id,
            "tokens_this_run": self.state.tokens_this_run,
            "max_tokens_per_run": self.config.max_tokens_per_run,
            "run_percent": round(run_pct, 1),
            "tokens_this_hour": self.state.tokens_this_hour,
            "max_tokens_per_hour": self.config.max_tokens_per_hour,
            "hour_percent": round(hour_pct, 1),
            "messages_this_run": self.state.messages_this_run,
            "hard_cap_triggered": self.state.hard_cap_triggered,
            "alerts_sent": self.state.alerts_sent,
            "registered_agents": list(self.state.agents.keys()),
            "agent_statuses": self.get_all_agent_statuses(),
        }

    def reset_run(self):
        self.state = BudgetState(run_id=datetime.now().strftime("%Y%m%d_%H%M%S"))
        logger.info(f"Token budget reset. New run: {self.state.run_id}")

    def configure(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
                logger.info(f"Budget config updated: {key} = {value}")
