"""
tests/unit/test_checkpoint.py - Unit tests for FP-10 Durable Mission State and Resumption Engine.
"""

import os
import tempfile
import pytest

from core.checkpoint import (
    CheckpointStore,
    CheckpointTamperedError,
    DurableMissionRunner,
    FaultInjectedCrashError,
    IdempotencyRegistry,
    MissionCancelledError,
    MissionCheckpoint,
    UnauthorizedResumeError,
)


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    yield path
    import gc
    gc.collect()
    try:
        if os.path.exists(path):
            os.remove(path)
    except PermissionError:
        pass


def test_checkpoint_signature_and_verification(temp_db_path: str) -> None:
    """Verify HMAC-SHA256 signature generation and constant-time integrity verification."""
    store = CheckpointStore(db_path=temp_db_path, hmac_secret=b"super_secret_test_key_12345")

    cp = MissionCheckpoint(
        mission_id="m_101",
        tenant_id="tenant_alpha",
        step_number=1,
        status="RUNNING",
        topology="pipeline",
        stage_index=0,
        accumulated_output="Step 1 raw data",
        budget_state={"tokens": 120, "steps": 1},
    )

    store.save_checkpoint(cp)
    assert cp.hmac_signature != ""
    assert cp.verify_integrity(b"super_secret_test_key_12345") is True

    # Loading from store should verify successfully
    loaded = store.load_latest_checkpoint("tenant_alpha", "m_101")
    assert loaded is not None
    assert loaded.accumulated_output == "Step 1 raw data"
    assert loaded.step_number == 1


def test_checkpoint_tamper_detection(temp_db_path: str) -> None:
    """Verify that tampering with checkpoint payload causes CheckpointTamperedError."""
    store = CheckpointStore(db_path=temp_db_path, hmac_secret=b"super_secret_test_key_12345")

    cp = MissionCheckpoint(
        mission_id="m_102",
        tenant_id="tenant_tamper",
        step_number=1,
        status="RUNNING",
        topology="orchestrator",
        stage_index=0,
        accumulated_output="Original uncompromised output",
    )
    store.save_checkpoint(cp)

    # Malicious direct DB manipulation (simulating DB injection or compromise)
    import sqlite3
    conn = sqlite3.connect(temp_db_path)
    try:
        conn.execute(
            """
            UPDATE mission_checkpoints
            SET accumulated_output = 'MALICIOUS INJECTED INSTRUCTION'
            WHERE tenant_id = 'tenant_tamper' AND mission_id = 'm_102'
            """
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(CheckpointTamperedError) as exc_info:
        store.load_latest_checkpoint("tenant_tamper", "m_102")

    assert "Checkpoint has been tampered with" in str(exc_info.value)


def test_idempotency_registry_deduplication(temp_db_path: str) -> None:
    """Verify that side-effecting operations are tracked and deduplicated per tenant/mission."""
    idem = IdempotencyRegistry(db_path=temp_db_path)
    tenant_id = "tenant_idemp"
    mission_id = "m_201"
    tool_name = "save_note"
    args = {"title": "Annual Report", "content": "Fiscal Q4 Net Profit +12%"}

    # 1. First check: not executed yet
    cached = idem.check_execution(tenant_id, mission_id, tool_name, args)
    assert cached is None

    # 2. Record execution
    key = idem.record_execution(tenant_id, mission_id, tool_name, args, "File written: /notes/annual_report.txt")
    assert key != ""

    # 3. Second check: must hit cache and return existing result
    cached_again = idem.check_execution(tenant_id, mission_id, tool_name, args)
    assert cached_again == "File written: /notes/annual_report.txt"

    # 4. Different arguments should not hit cache
    other_args = {"title": "Annual Report", "content": "Modified content"}
    assert idem.check_execution(tenant_id, mission_id, tool_name, other_args) is None


def test_reauthorization_on_resume(temp_db_path: str) -> None:
    """Verify that resumption re-checks tenant authorization before restoring state."""
    store = CheckpointStore(db_path=temp_db_path)
    tenant_id = "tenant_auth"
    mission_id = "m_301"

    cp = MissionCheckpoint(
        mission_id=mission_id,
        tenant_id=tenant_id,
        step_number=1,
        status="RUNNING",
        topology="peer",
        stage_index=0,
        accumulated_output="Progress",
    )
    store.save_checkpoint(cp)

    # Authorized resume succeeds
    loaded = store.load_latest_checkpoint(tenant_id, mission_id, auth_validator=lambda t: True)
    assert loaded is not None

    # Revoked / disabled tenant fails re-authorization
    with pytest.raises(UnauthorizedResumeError) as exc_info:
        store.load_latest_checkpoint(tenant_id, mission_id, auth_validator=lambda t: False)

    assert "failed authorization during mission resume" in str(exc_info.value)


def test_cancellation_lifecycle(temp_db_path: str) -> None:
    """Verify that cancelling a mission transitions status and prevents future execution."""
    store = CheckpointStore(db_path=temp_db_path)
    tenant_id = "tenant_cancel"
    mission_id = "m_401"

    runner = DurableMissionRunner(
        tenant_id=tenant_id,
        mission_id=mission_id,
        topology="pipeline",
        store=store,
    )

    stage_1 = lambda inp, s: inp + " -> Stage 1"
    runner.execute_stages([stage_1], "Start")

    # Cancel mission
    cancelled = store.cancel_mission(tenant_id, mission_id)
    assert cancelled is True

    # Attempt to continue/resume cancelled mission must fail fast
    with pytest.raises(MissionCancelledError) as exc_info:
        runner.execute_stages([stage_1], "Start")

    assert "was cancelled and cannot be resumed" in str(exc_info.value)


def test_fault_injection_and_resumption_with_zero_duplicate_side_effects(temp_db_path: str) -> None:
    """
    FAULT INJECTION TEST:
    1. Executes a multi-stage pipeline with a side-effecting operation (mock note save).
    2. Simulates an unhandled crash at Step 2 (prior to Step 2 commit).
    3. Re-launches the runner to resume the mission.
    4. Asserts that the mission successfully completes.
    5. Asserts that the side-effecting tool was executed EXACTLY ONCE (0 duplicate side-effects).
    """
    store = CheckpointStore(db_path=temp_db_path)
    tenant_id = "tenant_resilience"
    mission_id = "m_resilient_99"

    side_effect_execution_counter = 0

    def stage_one_with_side_effect(inp: str, chk_store: CheckpointStore) -> str:
        nonlocal side_effect_execution_counter
        # Side-effect check
        tool_args = {"file": "summary.txt", "data": "Stage 1 data"}
        cached = chk_store.idempotency.check_execution(tenant_id, mission_id, "save_note", tool_args)
        if cached is None:
            side_effect_execution_counter += 1
            res = f"Saved: {tool_args['file']}"
            chk_store.idempotency.record_execution(tenant_id, mission_id, "save_note", tool_args, res)
        else:
            res = cached
        return f"{inp} | Stage 1 Result ({res})"

    def stage_two(inp: str, chk_store: CheckpointStore) -> str:
        return f"{inp} | Stage 2 Analysis"

    def stage_three(inp: str, chk_store: CheckpointStore) -> str:
        return f"{inp} | Stage 3 Final Synthesis"

    stages = [stage_one_with_side_effect, stage_two, stage_three]

    runner = DurableMissionRunner(
        tenant_id=tenant_id,
        mission_id=mission_id,
        topology="pipeline",
        store=store,
    )

    # Run 1: Crash at Step 2
    with pytest.raises(FaultInjectedCrashError):
        runner.execute_stages(stages, "Task Initial Input", crash_at_step=2)

    # Verify Stage 1 side-effect ran once before crash
    assert side_effect_execution_counter == 1

    # Run 2: Resume crashed mission without fault injection
    final_output = runner.execute_stages(stages, "Task Initial Input", crash_at_step=None)

    # Verify complete output across all 3 stages
    assert "Task Initial Input | Stage 1 Result (Saved: summary.txt) | Stage 2 Analysis | Stage 3 Final Synthesis" == final_output

    # CRITICAL INVARIANT: Zero duplicate side-effects across fault recovery
    assert side_effect_execution_counter == 1

    # Verify checkpoint state is COMPLETED
    latest = store.load_latest_checkpoint(tenant_id, mission_id)
    assert latest is not None
    assert latest.status == "COMPLETED"
    assert latest.step_number == 3


def test_multi_tenant_isolation_and_cascading_purge(temp_db_path: str) -> None:
    """Verify cross-tenant checkpoint access is impossible and tenant purge cascades cleanly."""
    store = CheckpointStore(db_path=temp_db_path)
    tenant_a = "tenant_a"
    tenant_b = "tenant_b"

    cp_a = MissionCheckpoint(
        mission_id="mission_x",
        tenant_id=tenant_a,
        step_number=1,
        status="COMPLETED",
        topology="pipeline",
        stage_index=0,
        accumulated_output="Tenant A Classified Intel",
    )
    store.save_checkpoint(cp_a)

    # Tenant B tries to query Tenant A's mission -> returns None
    assert store.load_latest_checkpoint(tenant_b, "mission_x") is None

    # Purge Tenant A
    purged_count = store.purge_tenant_checkpoints(tenant_a)
    assert purged_count >= 1

    # Now Tenant A's checkpoint is gone
    assert store.load_latest_checkpoint(tenant_a, "mission_x") is None
