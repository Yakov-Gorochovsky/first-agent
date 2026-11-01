"""
core/meta_planner.py - Task-driven swarm design.

The planner has two jobs, kept deliberately separate:
1. The model analyses the task and proposes a team (structured output).
2. Deterministic code turns that analysis into a topology and enforces every rule that must hold:
   team size for the chosen topology, the privilege ceiling, and budget ceilings.

A manually chosen topology always wins over the task analysis.
"""

import hashlib
import logging
from typing import Any, Dict, Optional, Set, Tuple

from core.config import CONFIG
from core.factory import SwarmSpec
from core.llm import LLMProvider
from core.registry import GLOBAL_TOOL_REGISTRY, ToolRegistry
from core.security import wrap_untrusted_content
from core.structured import generate_structured_output
from core.topology import GLOBAL_TOPOLOGY_REGISTRY, TopologyRegistry

logger = logging.getLogger("neural_swarm.meta_planner")

# How the work in a task relates to itself, mapped to the topology that fits that shape.
WORK_SHAPE_TOPOLOGY: Dict[str, str] = {
    "atomic": "single",
    "independent_parts": "orchestrator",
    "sequential_stages": "pipeline",
    "draft_and_review": "review_loop",
    "interdependent": "peer",
}

# Inclusive (min, max) team size each topology can use well.
TOPOLOGY_TEAM_SIZE: Dict[str, Tuple[int, int]] = {
    "single": (1, 1),
    "orchestrator": (1, 6),
    "pipeline": (2, 6),
    "review_loop": (2, 2),
    "peer": (3, 6),
}

DEFAULT_CAPABILITIES: Set[str] = {"@web", "@code", "@files", "@memory"}
SELF_HEAL_CAPABILITY = "@self_heal"
MAX_ROLES = 6

CAPABILITY_GUIDE: Dict[str, str] = {
    "@web": "search the web and fetch public pages",
    "@code": "run Python in an offline sandbox and do exact calculations",
    "@files": "read and write shared workspace notes",
    "@memory": "store and recall tenant facts across missions",
    SELF_HEAL_CAPABILITY: "diagnose and repair this framework's own code (operator-authorized missions only)",
}

SWARM_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["title", "roles", "budgets"],
    "properties": {
        "title": {"type": "string", "description": "Short descriptive title of the team."},
        "task_analysis": {
            "type": "object",
            "required": ["complexity", "work_shape", "rationale"],
            "properties": {
                "domain": {"type": "string", "description": "Subject area of the task, in a few words."},
                "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]},
                "work_shape": {"type": "string", "enum": list(WORK_SHAPE_TOPOLOGY)},
                "rationale": {"type": "string", "description": "One sentence: why this shape fits the task."},
            },
        },
        "directive": {"type": "string", "description": "Overarching goal shared by all roles."},
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name", "purpose", "instructions", "capabilities"],
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "instructions": {"type": "string"},
                    "capabilities": {"type": "array", "items": {"type": "string"}},
                    "function": {"type": "string", "enum": ["discovery", "specialist", "verifier"]},
                    "limits": {"type": "object", "properties": {"max_steps": {"type": "integer"}}},
                },
            },
        },
        "budgets": {
            "type": "object",
            "required": ["max_swarm_steps", "max_total_tokens"],
            "properties": {
                "max_swarm_steps": {"type": "integer"},
                "max_total_tokens": {"type": "integer"},
                "max_mission_cost_usd": {"type": "number"},
            },
        },
    },
}


class PrivilegeCeilingError(PermissionError):
    """Raised when a swarm requests capabilities beyond the caller's authorization."""


def _sanitize_role(role: Dict[str, Any]) -> Dict[str, Any]:
    """Drop fields planners may not set, and raise any step limit to the working minimum."""
    clean = {key: value for key, value in role.items() if key not in {"tool_list", "model_policy", "max_steps"}}
    limits = dict(role.get("limits") or {})
    requested = limits.get("max_steps", role.get("max_steps"))
    if isinstance(requested, int):
        limits["max_steps"] = max(requested, CONFIG.min_role_steps)
    return {**clean, "limits": limits}


def resolve_topology(spec_dict: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """Return (topology, source) from a plan. The work shape takes precedence over a literal topology."""
    analysis = spec_dict.get("task_analysis")
    if isinstance(analysis, dict) and analysis.get("work_shape") in WORK_SHAPE_TOPOLOGY:
        return WORK_SHAPE_TOPOLOGY[analysis["work_shape"]], "task_analysis"
    topology = spec_dict.get("topology")
    return (str(topology), "spec") if topology else (None, "none")


class SwarmGenerationValidator:
    """Deterministic validator enforcing team shape, privilege ceiling, and budget limits."""

    def __init__(
        self,
        allowed_capabilities: Optional[Set[str]] = None,
        max_allowed_steps: int = 128,
        max_allowed_tokens: int = 500_000,
        max_allowed_cost_usd: float = 10.0,
        tool_registry: Optional[ToolRegistry] = None,
    ) -> None:
        self.allowed_capabilities = set(allowed_capabilities or DEFAULT_CAPABILITIES)
        self.max_allowed_steps = max_allowed_steps
        self.max_allowed_tokens = max_allowed_tokens
        self.max_allowed_cost_usd = max_allowed_cost_usd
        self.tool_registry = tool_registry or GLOBAL_TOOL_REGISTRY

    def validate_spec(
        self,
        spec_dict: Dict[str, Any],
        required_topology: Optional[str] = None,
        allowed_capabilities: Optional[Set[str]] = None,
    ) -> Tuple[bool, str]:
        roles = spec_dict.get("roles", [])
        if not roles:
            return False, "Generated swarm must contain at least one role."
        if len(roles) > MAX_ROLES:
            return False, f"Generated swarm requested {len(roles)} roles (max allowed: {MAX_ROLES})."

        for check in (
            self._check_capabilities(roles, allowed_capabilities or self.allowed_capabilities),
            self._check_team_shape(spec_dict, len(roles), required_topology),
            self._check_budgets(spec_dict.get("budgets", {})),
        ):
            if check:
                return False, check
        return True, ""

    @staticmethod
    def _check_team_shape(spec_dict: Dict[str, Any], count: int, required_topology: Optional[str]) -> str:
        if required_topology:
            # A human chose this topology; only reject teams it cannot physically run.
            if required_topology == "single" and count > 1:
                return "Topology 'single' runs exactly one role; merge the team into one role."
            return ""

        topology, _ = resolve_topology(spec_dict)
        if not topology:
            return "Plan must include task_analysis.work_shape so the topology can be chosen from the task."
        low, high = TOPOLOGY_TEAM_SIZE.get(topology, (1, MAX_ROLES))
        if not low <= count <= high:
            shape = (spec_dict.get("task_analysis") or {}).get("work_shape", topology)
            return (
                f"Team size mismatch: work shape '{shape}' uses topology '{topology}', which needs "
                f"{low}-{high} role(s), but the plan has {count}. Change the team or the work shape."
            )
        return ""

    @staticmethod
    def _check_capabilities(roles: Any, allowed: Set[str]) -> str:
        for role in roles:
            for cap in role.get("capabilities", []):
                norm_cap = str(cap).strip()
                if norm_cap not in allowed and f"@{norm_cap}" not in allowed:
                    name = role.get("name", "unknown")
                    return f"Privilege Ceiling Violation: Role '{name}' requested unauthorized capability '{cap}'."
        return ""

    def _check_budgets(self, budgets: Dict[str, Any]) -> str:
        steps = budgets.get("max_swarm_steps", 16)
        if steps > self.max_allowed_steps:
            return f"Budget Violation: Requested max_swarm_steps ({steps}) exceeds ceiling ({self.max_allowed_steps})."
        tokens = budgets.get("max_total_tokens", 24_000)
        if tokens > self.max_allowed_tokens:
            return f"Budget Violation: Requested max_total_tokens ({tokens}) exceeds ceiling ({self.max_allowed_tokens})."
        cost = budgets.get("max_mission_cost_usd", 1.0)
        if cost > self.max_allowed_cost_usd:
            return f"Budget Violation: Requested max_mission_cost_usd (${cost}) exceeds ceiling (${self.max_allowed_cost_usd})."
        return ""


def _system_prompt(allowed: Set[str], required_topology: Optional[str]) -> str:
    capabilities = "\n".join(
        f"- {cap}: {CAPABILITY_GUIDE.get(cap, 'registered capability')}" for cap in sorted(allowed)
    )
    choice = (
        f"The operator has fixed the topology to '{required_topology}'. Design the team for it.\n"
        if required_topology
        else (
            "Classify task_analysis.work_shape by how the work relates to itself:\n"
            "- atomic: one agent can do it all; there is nothing to divide. Use exactly 1 role.\n"
            "- independent_parts: separate parts that do not depend on each other. Use 1-6 roles.\n"
            "- sequential_stages: each stage transforms the previous stage's output. Use 2-6 roles.\n"
            "- draft_and_review: one artifact is produced and iteratively improved. Use exactly 2 roles: "
            "the producer first, then the reviewer.\n"
            "- interdependent: specialists must exchange findings as they work. Use 3-6 roles.\n"
        )
    )
    return (
        "You design agent teams for tasks in any field: writing, research, analysis, law, medicine, "
        "education, engineering, planning, or anything else.\n\n"
        f"{choice}\n"
        "Team rules:\n"
        "1. Use the smallest team that can do the task well. Most simple tasks need one agent.\n"
        "2. Name roles after what the task needs, in that field's own terms. Give each a distinct job.\n"
        "3. Give each role only the capabilities it will actually use. A role may have none.\n"
        "4. Set function to 'discovery' for a role that gathers facts others depend on, 'verifier' for one "
        "that checks others' work, and 'specialist' otherwise. Add these only when the task needs them.\n"
        "5. Set budgets in proportion to complexity.\n"
        "6. Return JSON matching the schema.\n\n"
        f"Available capabilities:\n{capabilities}"
    )


class TaskDrivenMetaPlanner:
    """Decomposes any task into a validated SwarmSpec."""

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

    def allowed_capabilities(self, allow_self_heal: bool) -> Set[str]:
        allowed = set(self.validator.allowed_capabilities)
        return allowed | {SELF_HEAL_CAPABILITY} if allow_self_heal else allowed

    def plan_swarm(
        self,
        task_prompt: str,
        tenant_id: str = "default",
        swarm_id: Optional[str] = None,
        required_topology: Optional[str] = None,
        allow_self_heal: bool = False,
    ) -> SwarmSpec:
        """Design a SwarmSpec for task_prompt. required_topology carries a manual choice."""
        allowed = self.allowed_capabilities(allow_self_heal)
        user_prompt = (
            "Design a team for the following mission:\n\n"
            f"{wrap_untrusted_content(source='user_mission_input', content=task_prompt)}\n\n"
            f"Allowed capabilities: {sorted(allowed)}"
        )
        if required_topology:
            user_prompt += f"\nRequired topology: {required_topology}"

        def validate(candidate: Dict[str, Any]) -> Tuple[bool, str]:
            return self.validator.validate_spec(candidate, required_topology, allowed)

        spec_data = generate_structured_output(
            llm=self.llm,
            prompt=user_prompt,
            schema=SWARM_SPEC_SCHEMA,
            system_prompt=_system_prompt(allowed, required_topology),
            max_retries=3,
            custom_validator=validate,
        )
        digest = hashlib.sha256(task_prompt.encode("utf-8")).hexdigest()[:10]
        return self.build_spec(
            spec_data,
            tenant_id=tenant_id,
            swarm_id=swarm_id or f"gen_{tenant_id}_{digest}",
            required_topology=required_topology,
        )

    def build_spec(
        self,
        data: Dict[str, Any],
        tenant_id: str,
        swarm_id: str,
        required_topology: Optional[str] = None,
    ) -> SwarmSpec:
        """Build a spec from untrusted data. Tool lists and model policy are never accepted from it."""
        roles = [_sanitize_role(role) for role in data.get("roles", []) if isinstance(role, dict)]
        topology, source = resolve_topology(data)
        if required_topology:
            topology, source = required_topology, "manual"
        spec = SwarmSpec.from_dict(
            {**data, "roles": roles, "swarm_id": swarm_id, "topology": topology},
            enforced_tenant_id=tenant_id,
        )
        spec.topology_source = source
        spec.validate(self.tool_registry)
        return spec
