"""
tests/unit/test_approval.py - FP-11 (Lite): Human Approval Gates & Anti-Bypass Security Tests.

Tests:
1. Normal approval workflow (request -> human approve -> single-use consume).
2. Action / Argument Tampering Detection (changed args after approval).
3. Replay Prevention (attempting to reuse an already consumed approval token).
4. Token Expiration (attempting to use an expired approval).
5. Operator Denial & Timeout (fail-closed behavior).
6. Approver Identity Verification.
"""

import time

import pytest

from core.approval import (
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalGate,
    ApprovalPolicy,
    ApprovalReplayError,
    ApprovalTamperingError,
    compute_arg_hash,
)


@pytest.mark.unit
def test_approval_gate_normal_workflow() -> None:
    """Standard flow: request created, approved by operator, verified and burned."""
    gate = ApprovalGate()

    args = {"filename": "output.txt", "content": "Hello World"}
    record = gate.request_approval(
        tenant_id="tenant_1",
        agent_name="writer",
        action="save_note",
        args=args,
    )
    assert record.status == "PENDING"
    assert record.arg_hash == compute_arg_hash(args)

    # Operator approves
    gate.approve(record.request_id, approver="alice@company.com", reason="Reviewed payload")
    assert record.status == "APPROVED"

    # Execution consumes the token
    gate.verify_and_burn_token(
        request_id=record.request_id,
        expected_action="save_note",
        submitted_args=args,
        expected_approver="alice@company.com",
    )
    assert record.status == "USED"


@pytest.mark.unit
def test_approval_argument_tampering_blocked() -> None:
    """Modifying arguments after approval causes immediate rejection with ApprovalTamperingError."""
    gate = ApprovalGate()

    original_args = {"code": "print('benign')"}
    record = gate.request_approval("tenant_1", "coder", "run_python", original_args)
    gate.approve(record.request_id, approver="sec_ops")

    # Attacker mutates code after human approval
    tampered_args = {"code": "import os; os.remove('/tmp/db')"}

    with pytest.raises(ApprovalTamperingError) as exc:
        gate.verify_and_burn_token(
            request_id=record.request_id,
            expected_action="run_python",
            submitted_args=tampered_args,
        )
    assert "Arguments tampered after approval" in str(exc.value)


@pytest.mark.unit
def test_approval_replay_blocked() -> None:
    """A single-use approval token cannot be submitted more than once."""
    gate = ApprovalGate()

    args = {"filename": "test.txt", "content": "123"}
    record = gate.request_approval("tenant_1", "writer", "save_note", args)
    gate.approve(record.request_id, approver="bob")

    # 1st redemption succeeds
    gate.verify_and_burn_token(record.request_id, "save_note", args)

    # 2nd redemption (replay) must fail
    with pytest.raises(ApprovalReplayError) as exc:
        gate.verify_and_burn_token(record.request_id, "save_note", args)
    assert "already been used" in str(exc.value)


@pytest.mark.unit
def test_approval_expiration_blocked() -> None:
    """Expired approvals cannot be approved or consumed."""
    policy = ApprovalPolicy(default_timeout_sec=0.1, token_ttl_sec=0.1)
    gate = ApprovalGate(policy=policy)

    args = {"query": "SELECT *"}
    record = gate.request_approval("tenant_1", "db_agent", "sql_query", args, timeout=0.1)

    time.sleep(0.15)  # Let it expire

    with pytest.raises(ApprovalExpiredError):
        gate.approve(record.request_id, approver="alice")


@pytest.mark.unit
def test_approval_denial_and_wrong_approver() -> None:
    """Operator denial and unauthorized approvers are strictly enforced."""
    gate = ApprovalGate()

    # Denial
    args = {"code": "rm -rf"}
    record = gate.request_approval("tenant_1", "coder", "run_python", args)
    gate.deny(record.request_id, approver="admin", reason="Malicious command")

    with pytest.raises(ApprovalDeniedError) as exc:
        gate.verify_and_burn_token(record.request_id, "run_python", args)
    assert "not in APPROVED state" in str(exc.value)

    # Wrong approver check
    args2 = {"code": "print(1)"}
    rec2 = gate.request_approval("tenant_1", "coder", "run_python", args2)
    gate.approve(rec2.request_id, approver="eve")

    with pytest.raises(ApprovalDeniedError) as exc:
        gate.verify_and_burn_token(
            rec2.request_id, "run_python", args2, expected_approver="authorized_admin"
        )
    assert "Approver mismatch" in str(exc.value)


@pytest.mark.unit
def test_worker_agent_live_approval_gate_interception(temp_storage) -> None:
    """WorkerAgent pauses on high-risk tool execution, emits approval_required, and resumes after human approval."""
    import threading
    from core.budget import BudgetConfig, BudgetTracker
    from core.bus import EventBus
    from core.llm import LLMResponse, ToolCallRequest
    from core.swarm import WorkerAgent
    from tests.conftest import ScriptedMockLLM

    gate = ApprovalGate(policy=ApprovalPolicy(high_risk="approve", default_timeout_sec=5.0))
    bus = EventBus()
    budget = BudgetTracker(config=BudgetConfig(), bus=bus)

    def _auto_operator(event) -> None:
        if event.event_type == "approval_required":
            req_id = event.data["request_id"]
            threading.Timer(0.05, lambda: gate.approve(req_id, approver="human_reviewer@org.com", reason="Verified safe")).start()

    bus.subscribe(_auto_operator)

    mock_llm = ScriptedMockLLM([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="tc_1",
                    name="run_python",
                    arguments={"code": "print(21 * 2)"},
                )
            ],
        ),
        LLMResponse(content="Approved calculation result: 42"),
    ])

    worker = WorkerAgent(
        name="coder",
        role="Engineer",
        instructions="Run python when needed.",
        llm=mock_llm,
        storage=temp_storage,
        budget=budget,
        bus=bus,
        tool_names=[],
        extra_tools={"run_python": lambda code: "STDOUT:\n42"},
        approval_gate=gate,
    )

    out = worker.execute_task("Compute 21 * 2 in python", tenant_id="tenant_live")
    assert "42" in out
    audit = gate.get_audit_trail(tenant_id="tenant_live")
    statuses = [a["status"] for a in audit]
    assert "PENDING" in statuses
    assert "APPROVED" in statuses
    assert "CONSUMED" in statuses
