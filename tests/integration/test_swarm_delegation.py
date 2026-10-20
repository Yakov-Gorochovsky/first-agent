"""
tests/integration/test_swarm_delegation.py - Integration tests for Orchestrator -> Worker delegation.
"""

import pytest

from core.budget import BudgetTracker
from core.bus import EventBus
from core.llm import LLMResponse, ToolCallRequest
from core.roles import RoleRegistry
from core.storage import SQLiteStorage
from core.swarm import OrchestratorSwarm
from tests.conftest import ScriptedMockLLM


@pytest.mark.integration
def test_orchestrator_delegation_flow(
    temp_storage: SQLiteStorage,
    event_bus: EventBus,
    budget_tracker: BudgetTracker,
    role_registry: RoleRegistry,
) -> None:
    """Orchestrator delegates a task to the coder worker and synthesizes the final reply."""
    responses = [
        # 1. Orchestrator calls delegate_to_worker(worker_name="coder", task="Calculate 15 * 15")
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="del_1",
                    name="delegate_to_worker",
                    arguments={"worker_name": "coder", "task": "Calculate 15 * 15"},
                )
            ],
            prompt_tokens=100,
            completion_tokens=25,
        ),
        # 2. Coder worker runs calculator
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="calc_1",
                    name="calculator",
                    arguments={"expression": "15 * 15"},
                )
            ],
            prompt_tokens=80,
            completion_tokens=20,
        ),
        # 3. Coder synthesizes result
        LLMResponse(
            content="15 * 15 is 225.",
            prompt_tokens=90,
            completion_tokens=10,
        ),
        # 4. Orchestrator provides final response
        LLMResponse(
            content="The coder specialist verified that 15 * 15 = 225.",
            prompt_tokens=110,
            completion_tokens=15,
        ),
    ]

    mock = ScriptedMockLLM(responses)
    orchestrator = OrchestratorSwarm(
        llm=mock,
        storage=temp_storage,
        budget=budget_tracker,
        bus=event_bus,
        role_registry=role_registry,
    )

    result = orchestrator.run("What is 15 * 15?", tenant_id="tenant_orch_1")
    assert "225" in result
    assert budget_tracker.total_steps >= 2
