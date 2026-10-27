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


@pytest.mark.integration
def test_peer_swarm_auto_fanout_and_priority_scheduler(
    temp_storage: SQLiteStorage,
    event_bus: EventBus,
    budget_tracker: BudgetTracker,
    tmp_path,
) -> None:
    """
    Verifies that when a planned team has >=3 roles (discovery + multiple domain specialists + critic),
    discovery's handoff automatically fans out to all unrun domain specialists and all domain specialists
    execute before the critic wakes up.
    """
    reg = RoleRegistry(
        roles_file=tmp_path / "roles.json",
        default_roles_file=tmp_path / "default_roles.json",
        ephemeral=True,
    )
    planned = [
        ("recon_specialist", "Recon Specialist", "Map endpoints"),
        ("security_auditor", "Security Auditor", "Probe JS and headers"),
        ("ux_rtl_specialist", "UX & RTL Specialist", "Audit RTL labels"),
        ("verification_critic", "Verification Critic", "Verify all proofs"),
    ]
    for r_name, r_title, r_inst in planned:
        reg.register_role(r_name, r_title, r_inst, capabilities=["@web", "@files"])

    execution_order = []

    class TrackingScriptedLLM(ScriptedMockLLM):
        def generate(self, messages=None, tools=None, **kwargs):
            sys_msg = (messages or [{}])[0].get("content", "")
            for r_name, _, _ in planned + [("coordinator", "", "")]:
                if f"You are '{r_name}'" in sys_msg:
                    execution_order.append(r_name)
                    break
            return super().generate(messages=messages, tools=tools, **kwargs)

    responses = [
        # 1. Coordinator -> recon_specialist
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="c1",
                    name="send_peer_message",
                    arguments={"to_agent": "recon_specialist", "message": "Run initial recon."},
                )
            ],
        ),
        # 2. recon_specialist -> only messages security_auditor (PeerSwarm auto-fans out to ux_rtl_specialist!)
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="c2",
                    name="send_peer_message",
                    arguments={"to_agent": "security_auditor", "message": "Manifest: V1 (JS), V2 (RTL)."},
                )
            ],
        ),
        # 3. security_auditor -> messages verification_critic (Tier 1 scheduler still runs ux_rtl_specialist next!)
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="c3",
                    name="send_peer_message",
                    arguments={"to_agent": "verification_critic", "message": "Security findings verified."},
                )
            ],
        ),
        # 4. ux_rtl_specialist -> messages verification_critic
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="c4",
                    name="send_peer_message",
                    arguments={"to_agent": "verification_critic", "message": "UX/RTL findings verified."},
                )
            ],
        ),
        # 5. verification_critic -> wakes up once with BOTH messages and finishes swarm
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="c5",
                    name="finish_swarm",
                    arguments={"final_answer": "Consolidated Executive Report: Security + UX/RTL verified."},
                )
            ],
        ),
    ]

    swarm = PeerSwarm(
        llm=TrackingScriptedLLM(responses),
        storage=temp_storage,
        budget=budget_tracker,
        bus=event_bus,
        role_registry=reg,
        allowed_roles=[r[0] for r in planned],
    )

    out = swarm.run("Audit target portal", tenant_id="tenant_fanout")
    assert out == "Consolidated Executive Report: Security + UX/RTL verified."
    assert execution_order == [
        "coordinator",
        "recon_specialist",
        "security_auditor",
        "ux_rtl_specialist",
        "verification_critic",
    ]
