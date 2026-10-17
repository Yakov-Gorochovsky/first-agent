"""
core/budget.py - Token, Step, and Time Budget Enforcement for Agent Swarms.

Prevents runaway multi-agent loops and tracks token usage across all workers.
"""

from dataclasses import dataclass, field
import time
from typing import Dict, Optional
from core.bus import EventBus
from core.config import CONFIG


class BudgetExceededError(RuntimeError):
    """Raised when an agent or the swarm exceeds its configured budget."""


@dataclass
class BudgetConfig:
    max_swarm_steps: int = field(default_factory=lambda: CONFIG.max_swarm_steps)
    max_steps_per_agent: int = field(default_factory=lambda: CONFIG.max_steps_per_agent)
    max_total_tokens: int = field(default_factory=lambda: CONFIG.max_total_tokens)
    max_wall_seconds: float = field(default_factory=lambda: CONFIG.max_wall_seconds)


@dataclass
class BudgetTracker:
    config: BudgetConfig = field(default_factory=BudgetConfig)
    bus: Optional[EventBus] = None
    tenant_id: str = "default"

    total_steps: int = 0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    agent_steps: Dict[str, int] = field(default_factory=dict)
    agent_limits: Dict[str, int] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)

    def reset(self) -> None:
        self.total_steps = 0
        self.total_tokens = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.agent_steps.clear()
        self.agent_limits.clear()
        self.start_time = time.time()

    @property
    def elapsed_seconds(self) -> float:
        return round(time.time() - self.start_time, 2)

    def check_and_increment_step(self, agent_name: str, max_steps: Optional[int] = None) -> None:
        """Verify step, token, and time limits before allowing an LLM call."""
        effective_agent_cap = max_steps if (max_steps is not None and max_steps > 0) else self.config.max_steps_per_agent
        self.agent_limits[agent_name] = effective_agent_cap

        if self.elapsed_seconds > self.config.max_wall_seconds:
            raise BudgetExceededError(
                f"Time budget exceeded ({self.elapsed_seconds}s > {self.config.max_wall_seconds}s)."
            )

        if self.total_steps >= self.config.max_swarm_steps:
            raise BudgetExceededError(
                f"Swarm step budget exceeded ({self.total_steps}/{self.config.max_swarm_steps} steps)."
            )

        current_agent_steps = self.agent_steps.get(agent_name, 0)
        if current_agent_steps >= effective_agent_cap:
            raise BudgetExceededError(
                f"Agent '{agent_name}' exceeded its step cap "
                f"({current_agent_steps}/{effective_agent_cap} steps)."
            )

        if self.total_tokens >= self.config.max_total_tokens:
            raise BudgetExceededError(
                f"Token budget exceeded ({self.total_tokens}/{self.config.max_total_tokens} tokens)."
            )

        self.total_steps += 1
        self.agent_steps[agent_name] = current_agent_steps + 1

    def record_tokens(self, agent_name: str, prompt_toks: int, completion_toks: int) -> None:
        """Record token consumption from an LLM completion and emit telemetry."""
        self.prompt_tokens += prompt_toks
        self.completion_tokens += completion_toks
        self.total_tokens += prompt_toks + completion_toks

        if self.bus:
            self.bus.emit(
                "budget",
                agent_name=agent_name,
                tenant_id=self.tenant_id,
                total_steps=self.total_steps,
                max_swarm_steps=self.config.max_swarm_steps,
                total_tokens=self.total_tokens,
                max_total_tokens=self.config.max_total_tokens,
                elapsed_seconds=self.elapsed_seconds,
            )

    def summary(self) -> Dict[str, object]:
        return {
            "total_steps": f"{self.total_steps}/{self.config.max_swarm_steps}",
            "total_tokens": f"{self.total_tokens}/{self.config.max_total_tokens}",
            "agent_steps": {
                k: f"{v}/{self.agent_limits.get(k, self.config.max_steps_per_agent)}"
                for k, v in self.agent_steps.items()
            },
            "elapsed_seconds": self.elapsed_seconds,
        }
