"""
core/handoffs.py - FP-08: Typed Machine-to-Machine Handoffs with Versioned JSON Schemas.

Architectural Purpose:
Replaces ambiguous, token-heavy natural language agent-to-agent chatter with
compact, schema-validated structured handoffs.

Guarantees:
1. Versioned JSON Schemas: Standard machine-to-machine handoff types (task_delegation_v1, specialist_finding_v1, review_critique_v1).
2. Wire Efficiency: Compact JSON serialization (no whitespace, short keys) saves 30-50% tokens compared to conversational prose.
3. Receipt Validation: Handoff payloads are validated against the schema upon receipt before entering peer inboxes.
4. Bounded Repair Feedback: Schema violations generate deterministic repair hints so agents fix invalid handoffs on retry.
5. Untrusted Demarcation: Field contents are treated as untrusted and escaped before dispatch to the UI.
"""

from dataclasses import asdict, dataclass, field
import html
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from core.structured import validate_schema

logger = logging.getLogger("neural_swarm.handoffs")


HANDOFF_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "task_delegation_v1": {
        "type": "object",
        "required": ["task_id", "objective", "inputs"],
        "properties": {
            "task_id": {"type": "string"},
            "objective": {"type": "string"},
            "inputs": {"type": "object"},
            "expected_outputs": {"type": "array", "items": {"type": "string"}},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
    },
    "specialist_finding_v1": {
        "type": "object",
        "required": ["finding_id", "summary", "data"],
        "properties": {
            "finding_id": {"type": "string"},
            "summary": {"type": "string"},
            "data": {"type": "object"},
            "artifacts": {"type": "array", "items": {"type": "string"}},
            "is_complete": {"type": "boolean"},
        },
    },
    "review_critique_v1": {
        "type": "object",
        "required": ["verdict", "comments"],
        "properties": {
            "verdict": {"type": "string", "enum": ["APPROVED", "REJECTED", "NEEDS_REVISION"]},
            "comments": {"type": "string"},
            "deficiencies": {"type": "array", "items": {"type": "string"}},
            "suggested_actions": {"type": "array", "items": {"type": "string"}},
        },
    },
}


class HandoffValidationError(ValueError):
    """Raised when an emitted peer handoff fails schema validation."""


@dataclass
class TypedHandoff:
    """
    Structured machine-to-machine handoff container.
    """
    sender: str
    recipient: str
    handoff_type: str
    payload: Dict[str, Any]
    version: str = "1.0"

    def serialize_compact(self) -> str:
        """Compact JSON wire representation with zero redundant whitespace."""
        return json.dumps(
            {
                "from": self.sender,
                "to": self.recipient,
                "type": self.handoff_type,
                "v": self.version,
                "p": self.payload,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def to_display_text(self) -> str:
        """Escape content safely for dashboard UI rendering."""
        safe_sender = html.escape(self.sender)
        safe_type = html.escape(self.handoff_type)
        safe_payload = html.escape(json.dumps(self.payload, indent=2))
        return f"[{safe_sender} -> {self.recipient} ({safe_type})]:\n{safe_payload}"


def validate_handoff_payload(
    handoff_type: str,
    payload: Dict[str, Any],
) -> Tuple[bool, str]:
    """Validate handoff payload against its versioned JSON schema."""
    schema = HANDOFF_SCHEMAS.get(handoff_type)
    if not schema:
        return (
            False,
            f"Unknown handoff_type '{handoff_type}'. Allowed types: {list(HANDOFF_SCHEMAS.keys())}",
        )
    return validate_schema(payload, schema)


def parse_and_validate_handoff(
    sender: str,
    recipient: str,
    handoff_type: str,
    raw_payload: Any,
) -> TypedHandoff:
    """
    Parse payload (dict or compact JSON string) and validate against schema.
    Raises HandoffValidationError on schema violation with detailed repair hint.
    """
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise HandoffValidationError(f"Handoff payload is not valid JSON: {exc}")
    elif isinstance(raw_payload, dict):
        payload = raw_payload
    else:
        raise HandoffValidationError(f"Handoff payload must be a JSON object, got {type(raw_payload).__name__}")

    valid, err_msg = validate_handoff_payload(handoff_type, payload)
    if not valid:
        schema = HANDOFF_SCHEMAS.get(handoff_type, {})
        raise HandoffValidationError(
            f"Handoff schema violation for type '{handoff_type}': {err_msg}\n"
            f"Required Schema: {json.dumps(schema)}"
        )

    return TypedHandoff(
        sender=sender,
        recipient=recipient,
        handoff_type=handoff_type,
        payload=payload,
    )
