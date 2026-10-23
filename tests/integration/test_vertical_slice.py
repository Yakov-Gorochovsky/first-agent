"""
tests/integration/test_vertical_slice.py - Integration Test for the Vertical Slice.

Verifies end-to-end integration across:
MetaPlanner -> SwarmFactory -> BaseTopology Execution -> ApprovalGate -> CheckpointStore.
"""

import json
from pathlib import Path

import pytest

from core.approval import ApprovalGate
from core.bus import EventBus
from core.checkpoint import CheckpointStore, CheckpointTamperedError
from core.factory import SwarmFactory
from core.llm import LLMResponse
from core.meta_planner import TaskDrivenMetaPlanner
from core.mission import IntegratedMissionRunner
from tests.conftest import ScriptedMockLLM


@pytest.mark.integration
def test_vertical_slice_end_to_end_lifecycle(temp_dir: Path) -> None:
    """
    Test complete lifecycle:
    1. MetaPlanner analyzes task prompt and emits a valid SwarmSpec JSON.
    2. SwarmFactory instantiates the topology.
    3. ApprovalGate issues a cryptographically strong token and authorizes high-risk tasks.
    4. Swarm runs and generates output.
    5. CheckpointStore persists HMAC-signed checkpoints at each step.
    6. Checkpoint integrity and anti-tamper are validated.
    """
    db_path = str(temp_dir / "vertical_slice.db")
    bus = EventBus()
    store = CheckpointStore(db_path=db_path, hmac_secret=b"vert_slice_test_secret_32bytes_entropy!")
    gate = ApprovalGate()

    planner_json = json.dumps({
        "swarm_id": "auto_swarm_sec",
        "title": "Security Audit Swarm",
        "topology": "orchestrator",
        "directive": "Coordinate security auditing and reporting.",
        "roles": [
            {
                "name": "sec_analyst",
                "purpose": "Perform static analysis of security components",
                "instructions": "Analyze logs and report security findings.",
                "capabilities": ["@code", "@files"],
                "max_steps": 3,
            }
        ],
        "budgets": {
            "max_swarm_steps": 8,
            "max_steps_per_agent": 3,
            "max_total_tokens": 5000,
            "max_wall_seconds": 60.0,
        },
    })

    # Response 1: MetaPlanner structured output
    # Response 2: Orchestrator swarm lead response
    # Response 3: Worker analyst response
    # Response 4: Orchestrator synthesis response
    mock_llm = ScriptedMockLLM([
        LLMResponse(content=planner_json),
        LLMResponse(content="Final synthesis: Security audit discovered zero critical vulnerabilities in workspace/."),
    ])

    meta_planner = TaskDrivenMetaPlanner(llm=mock_llm)
    factory = SwarmFactory()

    runner = IntegratedMissionRunner(
        llm=mock_llm,
        checkpoint_store=store,
        approval_gate=gate,
        swarm_factory=factory,
        meta_planner=meta_planner,
        bus=bus,
    )

    tenant = "client_corp"
    result = runner.execute(
        prompt="Perform a complete security review of authentication endpoints.",
        tenant_id=tenant,
        auto_approve_high_risk=True,
        approver="secops_lead@company.com",
    )

    # 1. Verify Result
    assert result.status == "COMPLETED"
    assert result.tenant_id == "client_corp"
    assert result.checkpoints_count == 3
    assert result.latest_checkpoint_hash is not None
    assert "Security audit" in result.output

    # 2. Verify Approval Gate Audit
    assert len(result.audit_events) >= 1
    assert result.audit_events[0]["status"] == "USED"
    assert result.audit_events[0]["approver"] == "secops_lead@company.com"

    # 3. Verify Checkpoints in CheckpointStore
    latest_cp = store.load_latest_checkpoint(tenant, result.mission_id)
    assert latest_cp is not None
    assert latest_cp.status == "COMPLETED"
    assert latest_cp.step_number == 3
    assert latest_cp.verify_integrity(b"vert_slice_test_secret_32bytes_entropy!") is True

    # 4. Verify Cryptographic Tamper Detection
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "UPDATE mission_checkpoints SET accumulated_output = 'TAMPERED INJECTION' "
            "WHERE tenant_id = ? AND mission_id = ?",
            (tenant, result.mission_id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(CheckpointTamperedError):
        store.load_latest_checkpoint(tenant, result.mission_id)
