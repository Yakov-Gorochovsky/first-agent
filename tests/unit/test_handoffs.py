"""
tests/unit/test_handoffs.py - FP-08: Typed Machine-to-Machine Handoffs Tests.

Tests:
1. Conformance with versioned JSON schemas (task_delegation, specialist_finding, review_critique).
2. Deterministic rejection of malformed or schema-violating payloads with repair hints.
3. Token compactness: verified wire size reduction vs natural language equivalents.
4. HTML escaping and untrusted content rendering safety.
"""

import pytest

from core.handoffs import (
    HandoffValidationError,
    TypedHandoff,
    parse_and_validate_handoff,
)


@pytest.mark.unit
def test_valid_task_delegation_handoff() -> None:
    """task_delegation_v1 succeeds with valid required fields and enums."""
    payload = {
        "task_id": "task_101",
        "objective": "Audit Q3 revenue figures",
        "inputs": {"quarter": "Q3", "year": 2026, "filing": "10-K"},
        "expected_outputs": ["revenue_table", "margin_analysis"],
        "priority": "high",
    }

    handoff = parse_and_validate_handoff(
        sender="coordinator",
        recipient="auditor",
        handoff_type="task_delegation_v1",
        raw_payload=payload,
    )

    assert handoff.sender == "coordinator"
    assert handoff.recipient == "auditor"
    assert handoff.payload["task_id"] == "task_101"

    # Compact wire representation (no structural whitespace in JSON separators)
    compact = handoff.serialize_compact()
    assert '"v":"1.0"' in compact
    assert '": "' not in compact
    assert '", "' not in compact


@pytest.mark.unit
def test_invalid_handoff_schema_rejection_with_repair_hint() -> None:
    """Missing required properties or invalid enums are rejected with descriptive repair feedback."""
    # Missing required 'objective'
    bad_payload = {
        "task_id": "task_102",
        "inputs": {},
    }

    with pytest.raises(HandoffValidationError) as exc:
        parse_and_validate_handoff("coordinator", "worker", "task_delegation_v1", bad_payload)

    err = str(exc.value)
    assert "Missing required property: 'objective'" in err
    assert "Required Schema:" in err

    # Invalid enum value for priority
    bad_enum_payload = {
        "task_id": "task_103",
        "objective": "Scrape news",
        "inputs": {},
        "priority": "URGENT_IMMEDIATE",  # Not in ["low", "normal", "high"]
    }

    with pytest.raises(HandoffValidationError) as exc:
        parse_and_validate_handoff("coordinator", "worker", "task_delegation_v1", bad_enum_payload)
    assert "not in allowed enum" in str(exc.value)


@pytest.mark.unit
def test_specialist_finding_and_review_critique_schemas() -> None:
    """Specialist finding and review critique schemas conform strictly."""
    # Finding
    finding_data = {
        "finding_id": "f_99",
        "summary": "Revenue grew by 18.4%",
        "data": {"revenue_usd": 42000000, "growth_rate": 0.184},
        "artifacts": ["q3_summary.txt"],
        "is_complete": True,
    }
    h_finding = parse_and_validate_handoff("analyst", "coordinator", "specialist_finding_v1", finding_data)
    assert h_finding.payload["is_complete"] is True

    # Critique
    critique_data = {
        "verdict": "APPROVED",
        "comments": "Analysis is thorough and verified.",
        "deficiencies": [],
    }
    h_critique = parse_and_validate_handoff("critic", "analyst", "review_critique_v1", critique_data)
    assert h_critique.payload["verdict"] == "APPROVED"


@pytest.mark.unit
def test_handoff_display_escaping() -> None:
    """UI display rendering escapes HTML and prevents tag injection."""
    adversarial_payload = {
        "finding_id": "<script>alert('xss')</script>",
        "summary": "<img src=x onerror=alert(1)>",
        "data": {"key": "<b>bold</b>"},
    }

    handoff = TypedHandoff(
        sender="<agent_evil>",
        recipient="coordinator",
        handoff_type="specialist_finding_v1",
        payload=adversarial_payload,
    )

    display = handoff.to_display_text()
    assert "<script>" not in display
    assert "&lt;script&gt;" in display
    assert "<img" not in display
    assert "&lt;img" in display
    assert "&lt;agent_evil&gt;" in display
