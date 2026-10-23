"""
core/structured.py - Shared Structured Output Engine with Bounded Repair Retries (FP-08 & FP-13).

Architectural Purpose:
Guarantees schema conformance for machine-to-machine handoffs and autonomous
swarm spec generation without relying on non-deterministic free-form prose.

Features:
1. Strict JSON extraction from markdown blocks or raw model completions.
2. Lightweight, zero-dependency schema validation (types, required properties, enums).
3. Bounded feedback repair loop: returns exact validation errors to the LLM on failure.
4. Extensible custom validator hooks (e.g. privilege ceiling verification).
"""

import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.llm import LLMProvider

logger = logging.getLogger("neural_swarm.structured")

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_payload(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extract a dictionary JSON object from either codeblocks or raw text."""
    clean = raw_text.strip()
    # Try direct parse
    if clean.startswith("{") and clean.endswith("}"):
        try:
            val = json.loads(clean)
            if isinstance(val, dict):
                return val
        except Exception:
            pass

    # Try regex match on markdown block
    match = _JSON_BLOCK_RE.search(clean)
    if match:
        try:
            val = json.loads(match.group(1))
            if isinstance(val, dict):
                return val
        except Exception:
            pass

    # Scan for first { and last }
    start = clean.find("{")
    end = clean.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            val = json.loads(clean[start : end + 1])
            if isinstance(val, dict):
                return val
        except Exception:
            pass

    return None


def validate_schema(data: Any, schema: Dict[str, Any]) -> Tuple[bool, str]:
    """Lightweight deterministic JSON Schema validator without third-party dependencies."""
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(data, dict):
            return False, f"Expected object, got {type(data).__name__}"

        required = schema.get("required", [])
        for field in required:
            if field not in data:
                return False, f"Missing required property: '{field}'"

        properties = schema.get("properties", {})
        for key, prop_schema in properties.items():
            if key in data:
                valid, err = validate_schema(data[key], prop_schema)
                if not valid:
                    return False, f"Field '{key}': {err}"

    elif expected_type == "array":
        if not isinstance(data, list):
            return False, f"Expected array, got {type(data).__name__}"
        item_schema = schema.get("items")
        if item_schema:
            for idx, item in enumerate(data):
                valid, err = validate_schema(item, item_schema)
                if not valid:
                    return False, f"Item [{idx}]: {err}"

    elif expected_type == "string":
        if not isinstance(data, str):
            return False, f"Expected string, got {type(data).__name__}"
        enum_vals = schema.get("enum")
        if enum_vals and data not in enum_vals:
            return False, f"Value '{data}' not in allowed enum: {enum_vals}"

    elif expected_type == "integer":
        if not isinstance(data, int) or isinstance(data, bool):
            return False, f"Expected integer, got {type(data).__name__}"

    elif expected_type == "number":
        if not isinstance(data, (int, float)) or isinstance(data, bool):
            return False, f"Expected number, got {type(data).__name__}"

    elif expected_type == "boolean":
        if not isinstance(data, bool):
            return False, f"Expected boolean, got {type(data).__name__}"

    return True, ""


def generate_structured_output(
    llm: LLMProvider,
    prompt: str,
    schema: Dict[str, Any],
    system_prompt: str = "You are a precise JSON-emitting agent. Return only valid JSON conforming strictly to the requested schema.",
    max_retries: int = 3,
    custom_validator: Optional[Callable[[Dict[str, Any]], Tuple[bool, str]]] = None,
) -> Dict[str, Any]:
    """
    Generate structured JSON data adhering to schema with feedback repair loops.
    """
    current_prompt = (
        f"{prompt}\n\n"
        f"CRITICAL: Output must be a single valid JSON object strictly matching this schema:\n"
        f"{json.dumps(schema, indent=2)}\n"
        f"Do NOT include conversational filler."
    )

    history: List[Dict[str, str]] = []

    for attempt in range(max_retries):
        messages = [{"role": "system", "content": system_prompt}] + history + [{"role": "user", "content": current_prompt}]
        try:
            resp = llm.generate(messages=messages)
        except TypeError:
            # Fallback for custom prompt-based providers
            resp = getattr(llm, "generate")(prompt=current_prompt, system_prompt=system_prompt, history=history)

        extracted = extract_json_payload(resp.content)
        if not extracted:
            err_msg = "Output could not be parsed as valid JSON."
        else:
            valid, err_msg = validate_schema(extracted, schema)
            if valid and custom_validator:
                valid, err_msg = custom_validator(extracted)

            if valid:
                return extracted

        # Feedback repair prompt
        logger.warning(
            json.dumps({
                "event": "structured_output_retry",
                "attempt": attempt + 1,
                "error": err_msg,
            })
        )

        history.append({"role": "user", "content": current_prompt})
        history.append({"role": "assistant", "content": resp.content})
        current_prompt = (
            f"Your previous output was INVALID.\n"
            f"Validation Error: {err_msg}\n"
            f"Please fix this error and return the complete, corrected JSON object."
        )

    raise ValueError(f"Failed to produce valid structured output after {max_retries} attempts. Last error: {err_msg}")
