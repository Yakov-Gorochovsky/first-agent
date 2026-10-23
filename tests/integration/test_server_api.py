"""
tests/integration/test_server_api.py - Integration tests for HTTP server security, SSE isolation, and host binding.
"""

import json
import queue

import pytest

from core.auth import AuthManager
from core.bus import SwarmEvent
from server import (
    SSEClientConnection,
    _broadcast_event,
    _sse_clients,
    _sse_clients_lock,
    check_host_binding_security,
)


@pytest.mark.integration
@pytest.mark.security
def test_host_binding_security(auth_manager: AuthManager) -> None:
    """Non-loopback bindings (0.0.0.0) must raise RuntimeError if authentication is disabled."""
    with pytest.raises(RuntimeError):
        check_host_binding_security("0.0.0.0", auth_enabled=False, auth_manager=auth_manager)

    # Allowed on localhost
    check_host_binding_security("127.0.0.1", auth_enabled=False, auth_manager=auth_manager)
    # Allowed when auth is enabled
    check_host_binding_security("0.0.0.0", auth_enabled=True, auth_manager=auth_manager)


@pytest.mark.integration
@pytest.mark.security
def test_sse_cross_tenant_isolation() -> None:
    """SSE broadcast must strictly route tenant events only to authorized client streams."""
    q_alpha: queue.Queue = queue.Queue(maxsize=10)
    q_beta: queue.Queue = queue.Queue(maxsize=10)
    q_admin: queue.Queue = queue.Queue(maxsize=10)

    conn_alpha = SSEClientConnection(client_queue=q_alpha, tenant_id="tenant_alpha", is_admin=False)
    conn_beta = SSEClientConnection(client_queue=q_beta, tenant_id="tenant_beta", is_admin=False)
    conn_admin = SSEClientConnection(client_queue=q_admin, tenant_id="admin", is_admin=True)

    with _sse_clients_lock:
        _sse_clients.extend([conn_alpha, conn_beta, conn_admin])

    try:
        # Emit an event for tenant_alpha
        evt = SwarmEvent(
            event_type="agent_start",
            agent_name="researcher",
            tenant_id="tenant_alpha",
            data={"secret_task": "Confidential alpha intelligence"},
        )
        _broadcast_event(evt)

        # Alpha client receives its event
        assert not q_alpha.empty()
        rec_alpha = json.loads(q_alpha.get_nowait())
        assert rec_alpha["tenant_id"] == "tenant_alpha"

        # Admin receives it
        assert not q_admin.empty()
        rec_admin = json.loads(q_admin.get_nowait())
        assert rec_admin["tenant_id"] == "tenant_alpha"

        # Beta client NEVER receives alpha's event
        assert q_beta.empty()
    finally:
        with _sse_clients_lock:
            for conn in (conn_alpha, conn_beta, conn_admin):
                if conn in _sse_clients:
                    _sse_clients.remove(conn)


@pytest.mark.integration
def test_version_endpoint_metadata() -> None:
    """The /api/version route returns a complete VersionInfo payload."""
    from core.version import get_version_info

    ver = get_version_info()
    data = ver.to_dict()
    assert "semver" in data
    assert "revision" in data
    assert "commit" in data
    assert "display_version" in data
    assert "server_instance_id" in data
    assert isinstance(data["is_dirty"], bool)

