"""
core/simulation.py - Emits a scripted peer-network event stream for UI demos, with no LLM calls.
"""

import time

from core.budget import BudgetTracker
from core.bus import EventBus
from core.roles import RoleRegistry
from core.storage import StorageBackend


def run_cinematic_simulation(
    bus: EventBus,
    budget: BudgetTracker,
    role_registry: RoleRegistry,
    storage: StorageBackend,
    tenant_id: str,
    prompt: str,
) -> None:
    """Emit a realistic multi-agent peer-network flow using active roles from RoleRegistry."""
    budget.reset()
    budget.tenant_id = tenant_id
    roles = role_registry.list_roles()
    r1 = roles[0].name if len(roles) > 0 else "researcher"
    r2 = roles[1].name if len(roles) > 1 else r1
    r3 = roles[-1].name if len(roles) > 2 else r2

    def step_sleep(sec: float = 0.85) -> None:
        time.sleep(sec)

    bus.emit(
        "peer_message",
        agent_name="user",
        tenant_id=tenant_id,
        from_agent="user",
        to_agent="coordinator",
        message=prompt,
        inboxes={"coordinator": 1},
    )
    step_sleep(0.7)

    bus.emit("agent_start", agent_name="coordinator", tenant_id=tenant_id, task=prompt)
    budget.check_and_increment_step("coordinator")
    budget.record_tokens("coordinator", 310, 95)
    step_sleep(0.8)

    bus.emit(
        "peer_message",
        agent_name="coordinator",
        tenant_id=tenant_id,
        from_agent="coordinator",
        to_agent=r1,
        message=f"Fetch live telemetry & benchmarks, then forward raw metrics directly to '{r2}' inbox.",
        inboxes={"coordinator": 0, r1: 1},
    )
    step_sleep(0.9)

    bus.emit("agent_start", agent_name=r1, tenant_id=tenant_id, task="Fetch live telemetry")
    budget.check_and_increment_step(r1)
    bus.emit(
        "tool_call",
        agent_name=r1,
        tenant_id=tenant_id,
        tool="web_search",
        arguments={"query": prompt[:60]},
    )
    step_sleep(0.9)
    bus.emit(
        "tool_result",
        agent_name=r1,
        tenant_id=tenant_id,
        tool="web_search",
        result="Found live benchmark data: latency_p95=42ms, throughput=14,200 req/s, temp=23C.",
    )
    budget.record_tokens(r1, 480, 140)
    step_sleep(0.7)

    bus.emit(
        "peer_message",
        agent_name=r1,
        tenant_id=tenant_id,
        from_agent=r1,
        to_agent=r2,
        message=f"Raw telemetry ready (p95=42ms, 14200 rps). Compute efficiency score and forward to '{r3}'.",
        inboxes={r1: 0, r2: 1},
    )
    step_sleep(0.9)

    bus.emit("agent_start", agent_name=r2, tenant_id=tenant_id, task="Run Python analysis")
    budget.check_and_increment_step(r2)
    bus.emit(
        "tool_call",
        agent_name=r2,
        tenant_id=tenant_id,
        tool="run_python",
        arguments={"code": "rps, p95 = 14200, 42\nscore = round((rps / p95) * 1.15, 2)\nprint(f'Efficiency Index: {score}')"},
    )
    step_sleep(0.9)
    bus.emit(
        "tool_result",
        agent_name=r2,
        tenant_id=tenant_id,
        tool="run_python",
        result="STDOUT:\nEfficiency Index: 388.81",
    )
    budget.record_tokens(r2, 520, 165)

    skill = storage.add_global_skill(
        category="telemetry_analysis",
        problem=f"Client {tenant_id} endpoint https://api.{tenant_id}.internal:8443 reported raw p95 metrics",
        solution="Normalize throughput-to-latency ratio (RPS / p95_ms) in Python sandbox before QA handoff.",
    )
    bus.emit("skill_learned", agent_name=r2, tenant_id=tenant_id, **skill)
    step_sleep(0.8)

    bus.emit(
        "peer_message",
        agent_name=r2,
        tenant_id=tenant_id,
        from_agent=r2,
        to_agent=r3,
        message="Computed Efficiency Index = 388.81 via Python sandbox. Please verify accuracy.",
        inboxes={r2: 0, r3: 1},
    )
    step_sleep(0.9)

    bus.emit("agent_start", agent_name=r3, tenant_id=tenant_id, task="Verify calculation")
    budget.check_and_increment_step(r3)
    bus.emit(
        "tool_call",
        agent_name=r3,
        tenant_id=tenant_id,
        tool="calculator",
        arguments={"expression": "(14200 / 42) * 1.15"},
    )
    step_sleep(0.7)
    bus.emit(
        "tool_result",
        agent_name=r3,
        tenant_id=tenant_id,
        tool="calculator",
        result="(14200 / 42) * 1.15 = 388.8095238095238",
    )
    budget.record_tokens(r3, 410, 110)
    step_sleep(0.6)

    final_text = (
        f"[Peer Swarm Verified Report for '{tenant_id}']\n"
        f"1. '{r1}' gathered live benchmark metrics (14,200 req/s @ 42ms p95).\n"
        f"2. '{r2}' executed Python sandbox analysis -> Efficiency Index: 388.81.\n"
        f"3. '{r3}' independently verified the math (388.8095) and approved the output.\n"
        "4. PrivacyAirlock distilled the reusable normalization rule into Shared Global Skills."
    )
    storage.save_tenant_memory(tenant_id, "last_efficiency_index", "388.81 (Verified)")
    storage.append_session_turn(tenant_id, "user", prompt)
    storage.append_session_turn(tenant_id, "assistant", final_text)

    bus.emit("swarm_complete", agent_name=r3, tenant_id=tenant_id, answer=final_text)
    bus.emit("agent_finish", agent_name=r3, tenant_id=tenant_id, answer=final_text)
