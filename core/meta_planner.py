"""
core/meta_planner.py - FP-13: Task-Driven Autonomous Swarm Generation (Meta-Planner).

Architectural Purpose:
Dynamically synthesizes specialized multi-agent swarms tailored to any task
across diverse domains (finance, medicine, software, document analysis, compliance)
without relying on rigid static hardcoded templates.

Security & Safety Guarantees:
1. Untrusted Mission Text Delimitation: Prevents indirect prompt injection in task prompts from seizing meta-planner control.
2. Privilege Ceiling Enforcement: Deterministic validator ensures generated roles cannot exceed the caller's authorized capabilities.
3. Structured Output Conformance: Uses FP-08/FP-13 schema validator with bounded repair loops.
4. Ephemeral-by-Default Lifecycle: Generated swarms are mission-scoped unless explicitly promoted to persistent libraries.
5. Strict Budget Bounds: Caps generated swarm step counts and token allocations to safety ceilings.
"""

import logging
from typing import Any, Dict, Optional, Set, Tuple

from core.factory import SwarmSpec
from core.llm import LLMProvider
from core.registry import GLOBAL_TOOL_REGISTRY, ToolRegistry
from core.security import wrap_untrusted_content
from core.structured import generate_structured_output
from core.topology import GLOBAL_TOPOLOGY_REGISTRY, TopologyRegistry

logger = logging.getLogger("neural_swarm.meta_planner")


SWARM_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["title", "topology", "roles", "budgets"],
    "properties": {
        "title": {
            "type": "string",
            "description": "Short descriptive title of the multi-agent swarm.",
        },
        "topology": {
            "type": "string",
            "enum": ["orchestrator", "peer", "pipeline", "review_loop"],
            "description": "Selected communication topology best suited for the task.",
        },
        "directive": {
            "type": "string",
            "description": "Overarching mission directive guiding all agents.",
        },
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "purpose", "instructions", "capabilities"],
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "instructions": {"type": "string"},
                    "capabilities": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "limits": {
                        "type": "object",
                        "properties": {
                            "max_steps": {"type": "integer"},
                        },
                    },
                },
            },
        },
        "budgets": {
            "type": "object",
            "required": ["max_swarm_steps", "max_total_tokens"],
            "properties": {
                "max_swarm_steps": {"type": "integer"},
                "max_total_tokens": {"type": "integer"},
                "max_wall_seconds": {"type": "number"},
                "max_mission_cost_usd": {"type": "number"},
            },
        },
    },
}


class PrivilegeCeilingError(PermissionError):
    """Raised when a generated swarm attempts to acquire capabilities exceeding caller authorization."""


class SwarmGenerationValidator:
    """Deterministic validator enforcing privilege ceiling, tool allow-lists, and budget limits."""

    def __init__(
        self,
        allowed_capabilities: Optional[Set[str]] = None,
        max_allowed_steps: int = 96,
        max_allowed_tokens: int = 160_000,
        max_allowed_cost_usd: float = 5.0,
        tool_registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.allowed_capabilities = allowed_capabilities or {"@web", "@code", "@files", "@memory"}
        self.max_allowed_steps = max_allowed_steps
        self.max_allowed_tokens = max_allowed_tokens
        self.max_allowed_cost_usd = max_allowed_cost_usd
        self.tool_registry = tool_registry or GLOBAL_TOOL_REGISTRY

    def validate_spec(self, spec_dict: Dict[str, Any]) -> Tuple[bool, str]:
        roles = spec_dict.get("roles", [])
        if not roles:
            return False, "Generated swarm must contain at least one role."
        if len(roles) > 6:
            return False, f"Generated swarm requested {len(roles)} roles (max allowed: 6)."

        # Check privilege ceiling on each role
        for role in roles:
            role_name = role.get("name", "unknown")
            caps = role.get("capabilities", [])
            for cap in caps:
                norm_cap = cap.strip()
                if norm_cap not in self.allowed_capabilities and f"@{norm_cap}" not in self.allowed_capabilities:
                    return False, f"Privilege Ceiling Violation: Role '{role_name}' requested unauthorized capability '{cap}'."

        # Check budget limits
        budgets = spec_dict.get("budgets", {})
        steps = budgets.get("max_swarm_steps", 16)
        if steps > self.max_allowed_steps:
            return False, f"Budget Violation: Requested max_swarm_steps ({steps}) exceeds ceiling ({self.max_allowed_steps})."

        tokens = budgets.get("max_total_tokens", 24_000)
        if tokens > self.max_allowed_tokens:
            return False, f"Budget Violation: Requested max_total_tokens ({tokens}) exceeds ceiling ({self.max_allowed_tokens})."

        cost = budgets.get("max_mission_cost_usd", 1.0)
        if cost > self.max_allowed_cost_usd:
            return False, f"Budget Violation: Requested max_mission_cost_usd (${cost}) exceeds ceiling (${self.max_allowed_cost_usd})."

        return True, ""


class TaskDrivenMetaPlanner:
    """
    Decomposes arbitrary high-level tasks and generates customized, validated SwarmSpecs.
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_registry: Optional[ToolRegistry] = None,
        topology_registry: Optional[TopologyRegistry] = None,
        validator: Optional[SwarmGenerationValidator] = None,
    ) -> None:
        self.llm = llm
        self.tool_registry = tool_registry or GLOBAL_TOOL_REGISTRY
        self.topology_registry = topology_registry or GLOBAL_TOPOLOGY_REGISTRY
        self.validator = validator or SwarmGenerationValidator(tool_registry=self.tool_registry)

    def plan_swarm(
        self,
        task_prompt: str,
        tenant_id: str = "default",
        swarm_id: Optional[str] = None,
    ) -> SwarmSpec:
        """
        Synthesize a SwarmSpec customized for task_prompt, verified against the privilege ceiling.
        """
        # Treat task prompt as untrusted input to privileged planner
        sanitized_input = wrap_untrusted_content(source="user_mission_input", content=task_prompt)

        system_prompt = (
            "You are an expert Multi-Agent Systems Architect. Your job is to decompose any task "
            "into a high-agency team of autonomous specialist agents that actively test hypotheses rather than writing passive summaries.\n\n"
            "Tool-Contract & Capability Rules:\n"
            "- '@web' (fetch_url, web_search): REQUIRED for any role that inspects live URLs, HTTP headers, endpoints, or raw HTML/DOM structure (`fetch_url(url, include_raw=True)`).\n"
            "- '@code' (run_python, calculator): Runs in an OFFLINE sandbox (no internet access). Use for computation, parsing workspace notes, regex/AST analysis, or local test scripts. If a role needs both live URL probing and Python analysis, give it BOTH '@web' and '@code'.\n"
            "- '@files' (save_note, read_note, list_notes) & '@memory' (remember_fact, recall_memories): Include on all roles so specialists share artifacts.\n\n"
            "Team Design Guidelines:\n"
            "1. Choose the most effective topology ('orchestrator', 'peer', 'pipeline', or 'review_loop').\n"
            "2. Design a Discovery -> Active Probing -> Verification workflow:\n"
            "   - Lead with a recon/discovery role (e.g. 'researcher') instructed to map the target empirically, research failure modes/attack vectors, and output a numbered Test Vector Manifest.\n"
            "   - Define domain specialists instructed to actively probe, test, or compute new evidence for each vector using their tools (never merely re-summarizing prior notes).\n"
            "   - Include a verification role ('critic') to reject generic textbook claims and verify empirical tool proof.\n"
            "3. Allocate generous step and token budgets (e.g., max_steps: 12-16 per role, max_swarm_steps: 64, max_total_tokens: 120000).\n"
            "4. Do NOT hallucinate capabilities outside ['@web', '@code', '@files', '@memory'].\n"
            "5. Return strictly conforming JSON adhering to the SwarmSpec schema."
        )

        user_prompt = (
            f"Generate a customized SwarmSpec for the following mission:\n\n"
            f"{sanitized_input}\n\n"
            f"Available Topologies: {self.topology_registry.list_topologies()}\n"
            f"Allowed Capabilities: {list(self.validator.allowed_capabilities)}"
        )

        spec_data = generate_structured_output(
            llm=self.llm,
            prompt=user_prompt,
            schema=SWARM_SPEC_SCHEMA,
            system_prompt=system_prompt,
            max_retries=3,
            custom_validator=self.validator.validate_spec,
        )

        # Build SwarmSpec instance
        effective_swarm_id = swarm_id or f"gen_{tenant_id}_{abs(hash(task_prompt)) % 100000}"
        spec_dict = {
            **spec_data,
            "swarm_id": effective_swarm_id,
            "tenant_id": tenant_id,
        }

        spec = SwarmSpec.from_dict(spec_dict, enforced_tenant_id=tenant_id)
        spec.validate(self.tool_registry)
        return spec
