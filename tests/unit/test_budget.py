"""
tests/unit/test_budget.py - Unit tests for step, token, and wall-clock budget guardrails.
"""

import time
import pytest

from core.budget import BudgetConfig, BudgetExceededError, BudgetTracker
from core.bus import EventBus


@pytest.mark.unit
def test_budget_step_cap_default(event_bus: EventBus) -> None:
    """Enforces max_steps_per_agent and max_swarm_steps caps."""
    config = BudgetConfig(max_swarm_steps=3, max_steps_per_agent=2, max_total_tokens=1000)
    tracker = BudgetTracker(config=config, bus=event_bus)

    tracker.check_and_increment_step("coder")
    tracker.check_and_increment_step("coder")

    with pytest.raises(BudgetExceededError) as exc_info:
        tracker.check_and_increment_step("coder")
    assert "exceeded its step cap" in str(exc_info.value)


@pytest.mark.unit
def test_budget_per_role_step_cap(event_bus: EventBus) -> None:
    """Specialist roles can have custom step caps passed to check_and_increment_step."""
    config = BudgetConfig(
        max_swarm_steps=10,
        max_steps_per_agent=2,
        max_total_tokens=5000,
    )
    tracker = BudgetTracker(config=config, bus=event_bus)

    for _ in range(4):
        tracker.check_and_increment_step("researcher", max_steps=4)

    with pytest.raises(BudgetExceededError) as exc:
        tracker.check_and_increment_step("researcher", max_steps=4)
    assert "exceeded its step cap (4/4 steps)" in str(exc.value)


@pytest.mark.unit
def test_budget_token_cap(event_bus: EventBus) -> None:
    """Accumulating tokens beyond max_total_tokens raises BudgetExceededError on next step check."""
    config = BudgetConfig(max_swarm_steps=10, max_steps_per_agent=5, max_total_tokens=100)
    tracker = BudgetTracker(config=config, bus=event_bus)

    tracker.record_tokens("coder", prompt_toks=50, completion_toks=40)
    assert tracker.total_tokens == 90

    tracker.record_tokens("coder", prompt_toks=10, completion_toks=10)  # total: 110 > 100
    assert tracker.total_tokens == 110

    with pytest.raises(BudgetExceededError) as exc:
        tracker.check_and_increment_step("coder")
    assert "Token budget exceeded" in str(exc.value)


@pytest.mark.unit
def test_budget_wall_clock_timeout(event_bus: EventBus) -> None:
    """Tracker detects expired wall-clock deadlines."""
    config = BudgetConfig(max_wall_seconds=0.01)
    tracker = BudgetTracker(config=config, bus=event_bus)
    time.sleep(0.02)

    with pytest.raises(BudgetExceededError) as exc:
        tracker.check_and_increment_step("coordinator")
    assert "Time budget exceeded" in str(exc.value)
