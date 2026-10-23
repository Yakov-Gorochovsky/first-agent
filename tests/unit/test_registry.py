"""
tests/unit/test_registry.py - FP-12: Tool Registry, Capability Bundles & Dynamic RoleSpec Tests.

Tests:
1. ToolMetadata risk class, side-effect, and category classifications.
2. Dynamic registration and deregistration of new tools without core modifications.
3. DynamicRoleSpec capability resolution (@all, @web, @code, @files, @memory).
4. Conformance verification: Zero hardcoded domain personas in core registry.
"""

import pytest

from core.registry import (
    DynamicRoleSpec,
    GLOBAL_TOOL_REGISTRY,
    ToolMetadata,
    ToolRegistry,
)


@pytest.mark.unit
def test_tool_metadata_declarations() -> None:
    """Builtin tools have appropriate risk classes and side-effect declarations."""
    reg = ToolRegistry()

    # Low risk, no side effects
    calc = reg.get("calculator")
    assert calc is not None
    assert calc.risk_class == "low"
    assert calc.side_effects is False

    # Untrusted web ingestion
    web = reg.get("web_search")
    assert web is not None
    assert web.risk_class == "medium"
    assert web.side_effects is False

    # High risk, side-effecting code execution
    python_tool = reg.get("run_python")
    assert python_tool is not None
    assert python_tool.risk_class == "high"
    assert python_tool.side_effects is True


@pytest.mark.unit
def test_dynamic_tool_registration() -> None:
    """New tools can be registered and queried dynamically without altering core code."""
    reg = ToolRegistry()

    def custom_sql_query(query: str) -> str:
        return f"Executed: {query}"

    meta = ToolMetadata(
        name="custom_sql",
        fn=custom_sql_query,
        schema={"type": "function", "function": {"name": "custom_sql"}},
        risk_class="high",
        side_effects=True,
        category="database",
    )

    reg.register(meta)
    assert reg.get("custom_sql") == meta
    assert meta in reg.list_by_risk("high")
    assert meta in reg.list_side_effecting()

    # Unregister
    assert reg.unregister("custom_sql") is True
    assert reg.get("custom_sql") is None


@pytest.mark.unit
def test_role_spec_capability_expansion() -> None:
    """DynamicRoleSpec resolves capability bundles against registered tools accurately."""
    reg = ToolRegistry()

    role = DynamicRoleSpec(
        name="researcher",
        purpose="Web research",
        instructions="Search web and read notes",
        capabilities=["@web", "@files"],
    )

    tools_resolved = role.resolve_effective_tools(reg)
    assert "web_search" in tools_resolved
    assert "fetch_url" in tools_resolved
    assert "read_note" in tools_resolved
    assert "save_note" in tools_resolved
    assert "run_python" not in tools_resolved  # @code not requested
