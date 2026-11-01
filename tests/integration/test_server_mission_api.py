"""
tests/integration/test_server_mission_api.py - HTTP-level authorization and governance for missions.

Drives the real request handler over a loopback socket with a stub mission runner and stub LLM.
"""

import json
import threading
from http.server import ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib import error, request

import pytest

import server
from core.approval import GLOBAL_APPROVAL_GATE
from core.auth import APIKeyRecord
from core.factory import SwarmSpec
from core.mission import MissionExecutionResult
from core.rate_limiter import RateLimitExceededError


class StubRunner:
    calls: List[Dict[str, Any]] = []

    def __init__(self, **kwargs: Any) -> None:
        pass

    def plan(self, prompt: str, tenant_id: str = "default", **kwargs: Any) -> SwarmSpec:
        StubRunner.calls.append({"plan": prompt, "tenant_id": tenant_id, **kwargs})
        return SwarmSpec.from_dict(
            {
                "swarm_id": "s1",
                "title": "Plan",
                "topology": "single",
                "roles": [{"name": "solo", "purpose": "p", "instructions": "i"}],
            },
            enforced_tenant_id=tenant_id,
        )

    def execute(self, prompt: str, tenant_id: str = "default", **kwargs: Any) -> MissionExecutionResult:
        StubRunner.calls.append({"prompt": prompt, "tenant_id": tenant_id, **kwargs})
        return MissionExecutionResult("m1", tenant_id, "COMPLETED", {"topology": "single"}, "ok", 3, "hash")


class StubAuth:
    records = {
        "user-a": APIKeyRecord(name="a", key_hash="", tenant_id="tenant_a", scopes=["user"]),
        "user-b": APIKeyRecord(name="b", key_hash="", tenant_id="tenant_b", scopes=["user"]),
        "admin": APIKeyRecord(name="root", key_hash="", tenant_id="admin", scopes=["admin", "user"]),
    }

    def verify_key(self, token: str) -> Optional[APIKeyRecord]:
        return self.records.get(token)

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: self.records.get(a[0]) if a else None


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[Tuple[str, Any]]:
    StubRunner.calls = []
    monkeypatch.setattr(server, "IntegratedMissionRunner", StubRunner)
    monkeypatch.setattr(server, "auth_manager", StubAuth())
    monkeypatch.setattr(server.CONFIG, "auth_enabled", True)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.SwarmRequestHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", server
    httpd.shutdown()
    httpd.server_close()


def post(base: str, path: str, body: Dict[str, Any], key: str) -> Tuple[int, Dict[str, Any]]:
    req = request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.security
def test_user_cannot_disable_approvals(api: Tuple[str, Any]) -> None:
    status, _ = post(api[0], "/api/missions/execute", {"prompt": "x", "auto_approve": True}, "user-a")
    assert status == 403
    assert StubRunner.calls == []


@pytest.mark.security
def test_admin_can_auto_approve(api: Tuple[str, Any]) -> None:
    status, _ = post(api[0], "/api/missions/execute", {"prompt": "x", "auto_approve": True}, "admin")
    assert status == 200
    assert StubRunner.calls[0]["auto_approve_high_risk"] is True


@pytest.mark.security
def test_user_cannot_request_self_heal(api: Tuple[str, Any]) -> None:
    status, _ = post(api[0], "/api/missions/execute", {"prompt": "x", "allow_self_heal": True}, "user-a")
    assert status == 403


@pytest.mark.security
def test_user_tenant_comes_from_the_credential(api: Tuple[str, Any]) -> None:
    post(api[0], "/api/missions/execute", {"prompt": "x", "tenant_id": "tenant_b"}, "user-a")
    assert StubRunner.calls[0]["tenant_id"] == "tenant_a"


@pytest.mark.security
def test_default_sync_path_is_rate_limited(api: Tuple[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(tenant_id: str) -> None:
        raise RateLimitExceededError("slow down", retry_after=1.0)

    monkeypatch.setattr(server.cost_governor, "check_rate_limit", deny)
    status, payload = post(api[0], "/api/missions/execute", {"prompt": "x"}, "user-a")
    assert status == 429 and "retry_after" in payload
    assert StubRunner.calls == []


@pytest.mark.security
def test_sync_path_releases_concurrency_slot(api: Tuple[str, Any]) -> None:
    for _ in range(5):
        status, _ = post(api[0], "/api/missions/execute", {"prompt": "x"}, "user-a")
        assert status == 200


@pytest.mark.security
def test_cross_tenant_approval_is_refused_over_http(api: Tuple[str, Any]) -> None:
    rec = GLOBAL_APPROVAL_GATE.request_approval("tenant_a", "agent", "run_python", {"code": "1"})
    status, _ = post(api[0], "/api/approvals/resolve", {"request_id": rec.request_id, "decision": "approve"}, "user-b")
    assert status == 404
    assert rec.status == "PENDING"
    status, _ = post(api[0], "/api/approvals/resolve", {"request_id": rec.request_id, "decision": "approve"}, "user-a")
    assert status == 200


@pytest.mark.integration
def test_plan_endpoint_returns_spec_without_running(api: Tuple[str, Any]) -> None:
    status, payload = post(api[0], "/api/missions/plan", {"prompt": "x", "mode": "auto"}, "user-a")
    assert status == 200
    assert payload["spec"]["topology"] == "single"
    assert "prompt" not in StubRunner.calls[0]


@pytest.mark.integration
def test_manual_mode_is_forwarded_and_auto_is_not(api: Tuple[str, Any]) -> None:
    post(api[0], "/api/missions/execute", {"prompt": "x", "mode": "pipeline"}, "user-a")
    post(api[0], "/api/missions/execute", {"prompt": "x", "mode": "auto"}, "user-a")
    assert StubRunner.calls[0]["topology_override"] == "pipeline"
    assert StubRunner.calls[1]["topology_override"] is None


@pytest.mark.security
def test_skill_approval_is_admin_only(api: Tuple[str, Any]) -> None:
    status, _ = post(api[0], "/api/skills/approve", {"skill_id": 1}, "user-a")
    assert status == 403
