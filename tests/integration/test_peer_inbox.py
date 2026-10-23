"""
tests/integration/test_peer_inbox.py - Integration tests for PeerSwarm decentralized handoffs and inbox queues.
"""

import pytest

from core.budget import BudgetTracker
from core.bus import EventBus
from core.llm import LLMResponse, ToolCallRequest
from core.peer_swarm import PeerSwarm
from core.roles import RoleRegistry
from core.storage import SQLiteStorage
from tests.conftest import ScriptedMockLLM


@pytest.mark.integration
def test_peer_swarm_multi_hop_handoff(
    temp_storage: SQLiteStorage,
    event_bus: EventBus,
    budget_tracker: BudgetTracker,
    role_registry: RoleRegistry,
) -> None:
    """Verifies PeerSwarm inbox message routing across Coordinator -> Coder -> Critic -> Finish."""
    responses = [
        # 1. Coordinator receives task and delegates to Coder
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_1",
                    name="send_peer_message",
                    arguments={"to_agent": "coder", "message": "Compute 9 * 9"},
                )
            ],
            prompt_tokens=80,
            completion_tokens=20,
        ),
        # 2. Coder computes and forwards to Critic
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_2",
                    name="calculator",
                    arguments={"expression": "9 * 9"},
                ),
                ToolCallRequest(
                    id="call_3",
                    name="send_peer_message",
                    arguments={"to_agent": "critic", "message": "Answer is 81. Confirm."},
                ),
            ],
            prompt_tokens=100,
            completion_tokens=25,
        ),
        # 3. Critic verifies and finishes swarm
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="call_4",
                    name="finish_swarm",
                    arguments={"final_answer": "Verified: 9 * 9 = 81."},
                )
            ],
            prompt_tokens=90,
            completion_tokens=15,
        ),
    ]

    mock = ScriptedMockLLM(responses)
    swarm = PeerSwarm(
        llm=mock,
        storage=temp_storage,
        budget=budget_tracker,
        bus=event_bus,
        role_registry=role_registry,
    )

    result = swarm.run("Calculate 9 * 9", tenant_id="tenant_int_1")
    assert result == "Verified: 9 * 9 = 81."
    assert budget_tracker.total_steps == 3
    assert budget_tracker.total_tokens == (80 + 20 + 100 + 25 + 90 + 15)


@pytest.mark.integration
def test_peer_swarm_exhausted_peer_shield(
    temp_storage: SQLiteStorage,
    event_bus: EventBus,
    budget_tracker: BudgetTracker,
    role_registry: RoleRegistry,
) -> None:
    """Verifies that send_peer_message warns when targeting a peer that already exceeded its step cap."""
    budget_tracker.config.max_steps_per_agent = 1
    # Simulate coder already using its step cap
    budget_tracker.agent_limits["coder"] = 1
    budget_tracker.agent_steps["coder"] = 1

    swarm = PeerSwarm(
        llm=ScriptedMockLLM([]),
        storage=temp_storage,
        budget=budget_tracker,
        bus=event_bus,
        role_registry=role_registry,
    )

    tools, _ = swarm._make_peer_comm_tools("coordinator", ["coordinator", "coder"])
    res = tools["send_peer_message"]("coder", "do more work")
    assert "Warning: Peer 'coder' has already reached its step cap" in res
    assert "Do NOT message 'coder'" in res


@pytest.mark.integration
def test_peer_swarm_ping_pong_circuit_breaker(
    temp_storage: SQLiteStorage,
    event_bus: EventBus,
    budget_tracker: BudgetTracker,
    role_registry: RoleRegistry,
) -> None:
    """Verifies that repetitive alternating handoffs trigger the SWARM CIRCUIT BREAKER."""
    swarm = PeerSwarm(
        llm=ScriptedMockLLM([]),
        storage=temp_storage,
        budget=budget_tracker,
        bus=event_bus,
        role_registry=role_registry,
    )

    coord_tools, _ = swarm._make_peer_comm_tools("coordinator", ["coordinator", "critic"])
    critic_tools, _ = swarm._make_peer_comm_tools("critic", ["coordinator", "critic"])

    # Simulate 4 alternating handoffs: C -> Cr -> C -> Cr
    coord_tools["send_peer_message"]("critic", "check 1")
    critic_tools["send_peer_message"]("coordinator", "empty 1")
    coord_tools["send_peer_message"]("critic", "check 2")
    critic_tools["send_peer_message"]("coordinator", "empty 2")

    # 5th attempt (coordinator -> critic again) should trip the circuit breaker
    res = coord_tools["send_peer_message"]("critic", "check 3")
    assert "[SWARM CIRCUIT BREAKER]" in res
    assert "Ping-pong loop detected between 'coordinator' and 'critic'" in res
