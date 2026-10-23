"""
tests/unit/test_roles.py - Unit tests for RoleRegistry and capability bundle expansion.
"""

from pathlib import Path

import pytest

from core.roles import RoleRegistry


@pytest.mark.unit
def test_default_roles_seeded(role_registry: RoleRegistry) -> None:
    """Default roles should include researcher, coder, critic, etc."""
    roles = role_registry.list_roles()
    role_names = [r.name for r in roles]
    assert "researcher" in role_names
    assert "coder" in role_names
    assert "critic" in role_names
    assert "security_reviewer" in role_names


@pytest.mark.unit
def test_capability_bundle_expansion(role_registry: RoleRegistry) -> None:
    """Capability bundles like @code and @files expand into concrete tool names."""
    custom = role_registry.register_role(
        name="script_builder",
        title="Script Builder",
        instructions="Write scripts.",
        capabilities=["@code", "@files"],
    )
    assert "run_python" in custom.tool_names
    assert "save_note" in custom.tool_names
    assert "read_note" in custom.tool_names
    assert "web_search" not in custom.tool_names  # @web not requested


@pytest.mark.unit
def test_role_persistence(temp_dir: Path) -> None:
    """Registered roles persist across registry instances backed by the same file."""
    roles_file = temp_dir / "persisted_roles.json"
    reg1 = RoleRegistry(roles_file=roles_file)
    reg1.register_role(
        name="custom_tester",
        title="Custom Tester",
        instructions="Testing stuff.",
        capabilities=["@files"],
    )

    reg2 = RoleRegistry(roles_file=roles_file)
    role = reg2.get_role("custom_tester")
    assert role is not None
    assert role.title == "Custom Tester"
    assert "save_note" in role.tool_names
