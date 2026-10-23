"""
tests/unit/test_storage.py - Unit tests for SQLite multi-tenant storage, indexing, and validation.
"""

import pytest

from core.storage import SQLiteStorage, validate_tenant_id


@pytest.mark.unit
@pytest.mark.security
def test_tenant_id_validation_strict() -> None:
    """Valid tenant IDs conform to ^[A-Za-z0-9_-]{1,64}$; invalid ones raise ValueError."""
    # Valid (normalizes to lowercase)
    assert validate_tenant_id("tenant_a") == "tenant_a"
    assert validate_tenant_id("Client-123") == "client-123"
    assert validate_tenant_id("alpha_beta_gamma") == "alpha_beta_gamma"

    # Reserved Windows/POSIX device names
    for reserved in ["CON", "con", "PRN", "AUX", "NUL", "COM1", "LPT1"]:
        with pytest.raises(ValueError) as exc_info:
            validate_tenant_id(reserved)
        assert "reserved system device name" in str(exc_info.value)

    # Invalid characters & traversal
    with pytest.raises(ValueError):
        validate_tenant_id("acme.corp")
    with pytest.raises(ValueError):
        validate_tenant_id("../etc/passwd")
    with pytest.raises(ValueError):
        validate_tenant_id("user@example.com")
    with pytest.raises(ValueError):
        validate_tenant_id("tenant/name")
    with pytest.raises(ValueError):
        validate_tenant_id("")
    with pytest.raises(ValueError):
        validate_tenant_id("a" * 65)  # oversized


@pytest.mark.unit
@pytest.mark.security
def test_multi_tenant_memory_isolation(temp_storage: SQLiteStorage) -> None:
    """Memories stored under tenant_a must be completely invisible to tenant_b."""
    temp_storage.save_tenant_memory("tenant_a", "api_token", "SECRET-A-999")
    temp_storage.save_tenant_memory("tenant_b", "api_token", "SECRET-B-888")

    mems_a = temp_storage.get_tenant_memories("tenant_a")
    assert mems_a.get("api_token") == "SECRET-A-999"

    mems_b = temp_storage.get_tenant_memories("tenant_b")
    assert mems_b.get("api_token") == "SECRET-B-888"
    assert "SECRET-A-999" not in mems_b.values()


@pytest.mark.unit
def test_session_turns_and_trace_logging(temp_storage: SQLiteStorage) -> None:
    """Session turns and swarm traces must log and retrieve in chronological order."""
    temp_storage.append_session_turn("client_1", "user", "Hello world")
    temp_storage.append_session_turn("client_1", "assistant", "Hi there!")

    turns = temp_storage.get_session_turns("client_1")
    assert len(turns) == 2
    assert turns[0]["role"] == "user"
    assert turns[0]["content"] == "Hello world"
    assert turns[1]["role"] == "assistant"
    assert turns[1]["content"] == "Hi there!"

    # Traces
    temp_storage.log_trace("client_1", "coder", "tool_call", {"op": "exec"})
    with temp_storage._connect() as conn:
        row = conn.execute("SELECT * FROM swarm_traces WHERE tenant_id = 'client_1'").fetchone()
        assert row is not None
        assert row["agent_name"] == "coder"
        assert row["tenant_id"] == "client_1"
