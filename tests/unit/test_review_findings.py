"""
tests/unit/test_review_findings.py - Regressions for bugs found in code review of the v0.7.0 work.
"""

from pathlib import Path
from typing import Any, Dict, List

import pytest

import server
from core.approval import ApprovalGate, ApprovalPolicy
from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.checkpoint import CheckpointStore
from core.llm import ToolCallRequest
from core.mission import IntegratedMissionRunner, _MissionLog
from core.security import TaintTracker
from core.storage import SQLiteStorage
from core.tool_runner import ToolRunner


def _runner(tmp_path: Path) -> IntegratedMissionRunner:
    return IntegratedMissionRunner(
        llm=object(),  # type: ignore[arg-type]
        checkpoint_store=CheckpointStore(db_path=str(tmp_path / "cp.db"), hmac_secret=b"x" * 32),
        storage=SQLiteStorage(db_path=str(tmp_path / "s.db")),
    )


def _plan(**overrides: Any) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "title": "t",
        "topology": "single",
        "roles": [{"name": "a", "purpose": "p", "instructions": "i", "capabilities": []}],
        "budgets": {"max_swarm_steps": 8, "max_total_tokens": 1000},
    }
    return {**plan, **overrides}


@pytest.mark.security
def test_mission_ignores_events_from_other_tenants(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    bus = EventBus()
    log = _MissionLog(runner.checkpoint_store, bus, "m1", "tenant_a")
    audit: List[Dict[str, Any]] = []
    bus.subscribe(runner._stage_listener(log, audit))

    bus.emit("agent_finish", agent_name="w", tenant_id="tenant_b", answer="B's private text")
    bus.emit("approval_resolved", agent_name="w", tenant_id="tenant_b", request_id="r", tool="x", status="APPROVED")
    assert log.step == 0 and audit == []

    bus.emit("agent_finish", agent_name="w", tenant_id="tenant_a", answer="ok")
    assert log.step == 1


@pytest.mark.integration
def test_unknown_preview_topology_is_rejected_before_the_mission_is_planned(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown topology"):
        _runner(tmp_path).execute("x", tenant_id="acme", preset_spec=_plan(topology="bogus"))


@pytest.mark.integration
@pytest.mark.parametrize(
    "overrides",
    [
        {"budgets": "x"},
        {"budgets": {"max_swarm_steps": "9"}},
        {"roles": ["x"]},
        {"roles": [{"name": "a", "capabilities": None}]},
        {"roles": [{"name": "a", "limits": "abc", "capabilities": []}]},
    ],
)
def test_malformed_preview_is_a_client_error(tmp_path: Path, overrides: Dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _runner(tmp_path).execute("x", tenant_id="acme", preset_spec=_plan(**overrides))


@pytest.mark.security
@pytest.mark.parametrize(
    "client_ip,expected",
    [("127.0.0.1", "the-key"), ("::1", "the-key"), ("192.168.1.20", ""), ("8.8.8.8", "")],
)
def test_admin_key_reaches_only_local_clients(monkeypatch: pytest.MonkeyPatch, client_ip: str, expected: str) -> None:
    monkeypatch.setattr(server.auth_manager, "active_admin_key", "the-key", raising=False)
    assert server.bootstrap_admin_key(client_ip) == expected


@pytest.mark.integration
def test_crash_in_an_approved_tool_is_not_reported_as_a_denial(tmp_path: Path) -> None:
    def crash(**_: Any) -> str:
        raise RuntimeError("disk full")

    bus = EventBus()
    gate = ApprovalGate(policy=ApprovalPolicy(high_risk="approve"))
    tool_runner = ToolRunner(
        agent_name="w",
        tenant_id="t",
        budget=BudgetTracker(config=BudgetConfig(), bus=bus, tenant_id="t"),
        bus=bus,
        storage=SQLiteStorage(db_path=str(tmp_path / "s.db")),
        taint_tracker=TaintTracker(),
        approval_gate=gate,
    )
    bus.subscribe(
        lambda event: gate.approve(event.data["request_id"], approver="human")
        if event.event_type == "approval_required"
        else None
    )
    result = tool_runner.run(ToolCallRequest(id="1", name="run_python", arguments={"code": "1"}), {"run_python": crash})
    assert "disk full" in result and "ApprovalDenied" not in result
