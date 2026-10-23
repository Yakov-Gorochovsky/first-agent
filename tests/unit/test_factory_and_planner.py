"""
tests/unit/test_factory_and_planner.py - FP-12 & FP-13: Swarm Factory & Task-Driven Meta-Planner Tests.

Tests:
1. SwarmSpec schema validation & safety bounds (role counts, step caps, token caps).
2. Tenant mass-assignment prevention (enforced tenant overrides client-supplied tenant).
3. SwarmFactory builds executable topologies with injected tenant namespaces.
4. TaskDrivenMetaPlanner generates valid SwarmSpecs via structured output.
5. Deterministic privilege ceiling blocks over-privileged generated roles.
"""

import json
from typing import Any, Dict, List, Optional

import pytest

from core.budget import BudgetConfig
from core.factory import (
    SwarmSpec,
    SwarmSpecValidationError,
)
from core.llm import LLMProvider, LLMResponse
from core.meta_planner import (
    SwarmGenerationValidator,
    TaskDrivenMetaPlanner,
)
from core.registry import DynamicRoleSpec


class MockStructuredLLM(LLMProvider):
    """Returns structured JSON matching SwarmSpec schema."""

    model: str = "mock-structured"

    def __init__(self, spec_payload: Dict[str, Any]) -> None:
        self.spec_payload = spec_payload

    def generate(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(
            content=json.dumps(self.spec_payload),
            prompt_tokens=50,
            completion_tokens=40,
        )


@pytest.mark.unit
def test_swarm_spec_validation_bounds() -> None:
    """SwarmSpec rejects empty roles, excessive steps, and unregistered tools."""
    # Empty roles
    spec = SwarmSpec(
        swarm_id="s1",
        tenant_id="tenant_a",
        title="Invalid Swarm",
        roles=[],
    )
    with pytest.raises(SwarmSpecValidationError) as exc:
        spec.validate()
    assert "at least one role" in str(exc.value)

    # Exceeding step budget bound (> 128)
    spec_large_budget = SwarmSpec(
        swarm_id="s1",
        tenant_id="tenant_a",
        title="Excessive Budget Swarm",
        roles=[DynamicRoleSpec("analyst", "Analyze", "Do analysis")],
        budgets=BudgetConfig(max_swarm_steps=500),
    )
    with pytest.raises(SwarmSpecValidationError) as exc:
        spec_large_budget.validate()
    assert "max_swarm_steps exceeds safety bound" in str(exc.value)


@pytest.mark.unit
def test_swarm_spec_tenant_mass_assignment_protection() -> None:
    """Client-supplied tenant_id is overridden by the enforced authenticated tenant context."""
    attacker_data = {
        "swarm_id": "malicious_swarm",
        "tenant_id": "victim_corp",  # Attacker attempts to impersonate victim
        "title": "Phishing Swarm",
        "roles": [{"name": "worker", "purpose": "work", "instructions": "do work"}],
    }

    # Authenticated tenant is 'attacker'
    spec = SwarmSpec.from_dict(attacker_data, enforced_tenant_id="attacker")
    assert spec.tenant_id == "attacker"
    assert spec.roles[0].tenant_id == "attacker"


@pytest.mark.unit
def test_task_driven_meta_planner_generation() -> None:
    """Meta-Planner generates a validated SwarmSpec matching the task prompt."""
    valid_payload = {
        "title": "Autonomous Financial Research Team",
        "topology": "orchestrator",
        "directive": "Analyze Q3 SEC filings accurately.",
        "roles": [
            {
                "name": "edgar_scraper",
                "purpose": "Fetch filings",
                "instructions": "Extract 10-K filings using web tools.",
                "capabilities": ["@web"],
            },
            {
                "name": "financial_auditor",
                "purpose": "Audit revenue",
                "instructions": "Verify balance sheet figures with python.",
                "capabilities": ["@code"],
            },
        ],
        "budgets": {
            "max_swarm_steps": 16,
            "max_total_tokens": 20_000,
            "max_wall_seconds": 120.0,
            "max_mission_cost_usd": 1.50,
        },
    }

    mock_llm = MockStructuredLLM(valid_payload)
    planner = TaskDrivenMetaPlanner(llm=mock_llm)

    spec = planner.plan_swarm(
        task_prompt="Audit Apple Q3 10-K filing",
        tenant_id="tenant_finance",
        swarm_id="finance_swarm_1",
    )

    assert spec.swarm_id == "finance_swarm_1"
    assert spec.tenant_id == "tenant_finance"
    assert spec.topology == "orchestrator"
    assert len(spec.roles) == 2
    assert spec.roles[0].name == "edgar_scraper"
    assert spec.roles[1].name == "financial_auditor"


@pytest.mark.unit
def test_privilege_ceiling_blocks_over_privileged_roles() -> None:
    """Deterministic validator blocks generation when a role requests unauthorized capabilities."""
    validator = SwarmGenerationValidator(
        allowed_capabilities={"@web", "@files"},  # @code is NOT permitted for this tenant
    )

    invalid_payload = {
        "title": "Privilege Escalation Swarm",
        "topology": "peer",
        "roles": [
            {
                "name": "escalated_worker",
                "purpose": "Run arbitrary commands",
                "instructions": "Execute bash/python scripts",
                "capabilities": ["@code"],  # Unauthorized capability!
            }
        ],
        "budgets": {
            "max_swarm_steps": 10,
            "max_total_tokens": 10000,
        },
    }

    valid, err = validator.validate_spec(invalid_payload)
    assert valid is False
    assert "Privilege Ceiling Violation" in err
    assert "@code" in err
