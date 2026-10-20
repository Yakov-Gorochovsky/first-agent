"""
tests/unit/test_auth.py - Unit tests for API key verification and role-based access control.
"""

from pathlib import Path
import json
import pytest

from core.auth import APIKeyRecord, AuthManager


@pytest.mark.unit
@pytest.mark.security
def test_default_keys_loaded_and_verified(auth_manager: AuthManager) -> None:
    """Default admin and client keys must verify correctly with expected scopes."""
    admin_rec = auth_manager.verify_key("ns-admin-secret-key-12345")
    assert admin_rec is not None
    assert admin_rec.has_scope("admin") is True
    assert admin_rec.has_scope("user") is True
    assert admin_rec.tenant_id == "admin"

    client_rec = auth_manager.verify_key("ns-client-a-secret-key-12345")
    assert client_rec is not None
    assert client_rec.has_scope("user") is True
    assert client_rec.has_scope("admin") is False
    assert client_rec.tenant_id == "client_a"


@pytest.mark.unit
@pytest.mark.security
def test_invalid_or_tampered_key_rejected(auth_manager: AuthManager) -> None:
    """Invalid, empty, or whitespace tokens must return None in constant-time."""
    assert auth_manager.verify_key("tampered-token-999") is None
    assert auth_manager.verify_key("") is None
    assert auth_manager.verify_key("   ") is None
    assert auth_manager.verify_key("ns-admin-secret-key-1234") is None  # truncated


@pytest.mark.unit
def test_generate_and_verify_custom_key(auth_manager: AuthManager) -> None:
    """Dynamically created keys should verify and can be listed."""
    raw_token, rec = auth_manager.create_key(
        name="Tenant X Test Key",
        tenant_id="tenant_x",
        scopes=["user"],
    )
    assert raw_token.startswith("ns_")
    assert rec.tenant_id == "tenant_x"
    assert rec.has_scope("user") is True

    # Verification passes with newly generated raw token
    verified = auth_manager.verify_key(raw_token)
    assert verified is not None
    assert verified.name == rec.name
    assert verified.tenant_id == "tenant_x"

    records = auth_manager.list_records()
    assert any(r["tenant_id"] == "tenant_x" for r in records)


@pytest.mark.unit
@pytest.mark.security
def test_corrupted_keyfile_triggers_random_fallback(temp_dir: Path) -> None:
    """If keys.json is corrupted with invalid JSON, AuthManager must fall back to random admin key."""
    corrupt_file = temp_dir / "corrupt_keys.json"
    corrupt_file.write_text("{ this is not valid json }", encoding="utf-8")

    mgr = AuthManager(keys_file=corrupt_file)
    records = mgr.list_records()
    assert len(records) >= 1
    assert "admin" in records[0]["scopes"]
