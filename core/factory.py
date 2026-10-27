"""
core/factory.py - FP-12: Role, Agent, and Swarm Factory with Lifecycle & Authorization Controls.

Architectural Purpose:
Enables dynamic instantiation of arbitrary agent teams for any domain.
Guarantees:
1. Roles and Swarms as Pure Data: No domain personas hardcoded in core orchestration.
2. Capability Resolution & Least Privilege: Maps abstract capabilities to validated tools in ToolRegistry.
3. Strict Multi-Tenant Authorization: Operations are strictly isolated by tenant_id. Cross-tenant queries return 404/None.
4. Client Mass-Assignment Protection: Owner/tenant fields supplied by client requests are ignored and bound to auth context.
5. Pluggable Topology Binding: Resolves swarms dynamically via TopologyRegistry.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.approval import ApprovalGate
from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider
from core.registry import GLOBAL_TOOL_REGISTRY, DynamicRoleSpec, ToolRegistry
from core.router import ModelPolicyRouter, RoleRoutingConfig, RoutingLLMProvider
from core.security import TaintTracker
from core.storage import sanitize_tenant_id, validate_tenant_id
from core.swarm import WorkerAgent
from core.topology import GLOBAL_TOPOLOGY_REGISTRY, BaseTopology, TopologyRegistry

logger = logging.getLogger("neural_swarm.factory")


class SwarmSpecValidationError(ValueError):
    """Raised when a SwarmSpec violates security constraints or allowable bounds."""


@dataclass
class SwarmSpec:
    """
    FP-12 Versioned Specification defining a multi-agent swarm team.
    """
    swarm_id: str
    tenant_id: str
    title: str
    topology: str = "peer"  # "orchestrator" | "peer" | "pipeline" | "review_loop"
    roles: List[DynamicRoleSpec] = field(default_factory=list)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    policy_refs: List[str] = field(default_factory=list)
    version: int = 1
    color: str = "#38bdf8"
    directive: str = ""

    def validate(self, tool_registry: Optional[ToolRegistry] = None) -> None:
        """Enforce bounds on role counts, step budgets, and registered tools."""
        t_reg = tool_registry or GLOBAL_TOOL_REGISTRY

        # Tenant & Swarm ID validation
        validate_tenant_id(self.tenant_id)
        validate_tenant_id(self.swarm_id)

        if not self.title.strip():
            raise SwarmSpecValidationError("Swarm title cannot be empty.")

        # Bounds checks
        if len(self.roles) == 0:
            raise SwarmSpecValidationError("Swarm must specify at least one role.")
        if len(self.roles) > 12:
            raise SwarmSpecValidationError(f"Swarm exceeds maximum allowed roles (got {len(self.roles)}, max 12).")

        if self.budgets.max_swarm_steps > 128:
            raise SwarmSpecValidationError(f"max_swarm_steps exceeds safety bound ({self.budgets.max_swarm_steps} > 128).")
        if self.budgets.max_total_tokens > 500_000:
            raise SwarmSpecValidationError(f"max_total_tokens exceeds safety bound ({self.budgets.max_total_tokens} > 500,000).")

        # Validate tools for all roles
        for r in self.roles:
            if not r.name.strip():
                raise SwarmSpecValidationError("Role name cannot be empty.")
            effective_tools = r.resolve_effective_tools(t_reg)
            for t_name in effective_tools:
                if not t_reg.get(t_name):
                    raise SwarmSpecValidationError(f"Role '{r.name}' requested unregistered tool '{t_name}'.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "swarm_id": self.swarm_id,
            "tenant_id": self.tenant_id,
            "title": self.title,
            "topology": self.topology,
            "roles": [r.to_dict() for r in self.roles],
            "budgets": {
                "max_swarm_steps": self.budgets.max_swarm_steps,
                "max_steps_per_agent": self.budgets.max_steps_per_agent,
                "max_total_tokens": self.budgets.max_total_tokens,
                "max_wall_seconds": self.budgets.max_wall_seconds,
                "max_mission_cost_usd": self.budgets.max_mission_cost_usd,
            },
            "policy_refs": self.policy_refs,
            "version": self.version,
            "color": self.color,
            "directive": self.directive,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], enforced_tenant_id: Optional[str] = None) -> "SwarmSpec":
        """Build spec with protection against tenant mass assignment."""
        raw_tenant = enforced_tenant_id or data.get("tenant_id", "default")
        safe_tenant = sanitize_tenant_id(str(raw_tenant))

        roles_raw = data.get("roles", [])
        parsed_roles = [
            DynamicRoleSpec.from_dict({**r, "tenant_id": safe_tenant})
            for r in roles_raw
            if isinstance(r, dict)
        ]

        budgets_raw = data.get("budgets", {})
        budget_cfg = BudgetConfig(
            max_swarm_steps=int(budgets_raw.get("max_swarm_steps", CONFIG.max_swarm_steps)),
            max_steps_per_agent=int(budgets_raw.get("max_steps_per_agent", CONFIG.max_steps_per_agent)),
            max_total_tokens=int(budgets_raw.get("max_total_tokens", CONFIG.max_total_tokens)),
            max_wall_seconds=float(budgets_raw.get("max_wall_seconds", CONFIG.max_wall_seconds)),
            max_mission_cost_usd=float(budgets_raw.get("max_mission_cost_usd", 2.0)),
        )

        return cls(
            swarm_id=str(data.get("swarm_id", "swarm_default")).strip().lower(),
            tenant_id=safe_tenant,
            title=str(data.get("title", "Dynamic Swarm")).strip(),
            topology=str(data.get("topology", "peer")).strip().lower(),
            roles=parsed_roles,
            budgets=budget_cfg,
            policy_refs=list(data.get("policy_refs", [])),
            version=int(data.get("version", 1)),
            color=str(data.get("color", "#38bdf8")),
            directive=str(data.get("directive", "")).strip(),
        )


class AgentFactory:
    """Instantiates worker agents from declarative RoleSpecs with scoped namespaces."""

    def __init__(
        self,
        tool_registry: Optional[ToolRegistry] = None,
        router: Optional[ModelPolicyRouter] = None,
        provider_instances: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.tool_registry = tool_registry or GLOBAL_TOOL_REGISTRY
        self.router = router
        self.provider_instances = provider_instances or {}

    def create_agent(
        self,
        role_spec: DynamicRoleSpec,
        llm: LLMProvider,
        budget: BudgetTracker,
        bus: Optional[EventBus] = None,
        taint_tracker: Optional[TaintTracker] = None,
        storage: Optional[Any] = None,
        approval_gate: Optional[ApprovalGate] = None,
    ) -> WorkerAgent:
        """Create a configured WorkerAgent bound to tenant isolation boundary."""
        tools_list = role_spec.resolve_effective_tools(self.tool_registry)
        from core.storage import SQLiteStorage
        active_storage = storage or SQLiteStorage()
        active_bus = bus or EventBus()

        effective_llm = llm
        if self.router and role_spec.model_policy:
            p_prov = role_spec.model_policy.get("primary_provider") or role_spec.model_policy.get("provider")
            p_mod = role_spec.model_policy.get("primary_model") or role_spec.model_policy.get("model")
            if p_prov and p_mod:
                fb = role_spec.model_policy.get("fallback_chain", [])
                fb_tuples = [
                    (item.get("provider", ""), item.get("model", ""))
                    if isinstance(item, dict) else item
                    for item in fb
                ]
                r_cfg = RoleRoutingConfig(
                    primary_provider=p_prov,
                    primary_model=p_mod,
                    fallback_chain=fb_tuples,
                )
                effective_llm = RoutingLLMProvider(
                    tenant_id=role_spec.tenant_id,
                    routing_config=r_cfg,
                    router=self.router,
                    provider_instances=self.provider_instances,
                )

        return WorkerAgent(
            name=role_spec.name,
            role=role_spec.name.title(),
            instructions=role_spec.instructions,
            llm=effective_llm,
            storage=active_storage,
            budget=budget,
            bus=active_bus,
            tool_names=tools_list,
            max_steps=role_spec.max_steps,
            taint_tracker=taint_tracker,
            approval_gate=approval_gate,
        )


class SwarmFactory:
    """Instantiates and executes swarms from SwarmSpec configurations."""

    def __init__(
        self,
        topology_registry: Optional[TopologyRegistry] = None,
        tool_registry: Optional[ToolRegistry] = None,
        agent_factory: Optional[AgentFactory] = None,
    ) -> None:
        self.topology_registry = topology_registry or GLOBAL_TOPOLOGY_REGISTRY
        self.tool_registry = tool_registry or GLOBAL_TOOL_REGISTRY
        self.agent_factory = agent_factory or AgentFactory(self.tool_registry)

    def build_topology(
        self,
        spec: SwarmSpec,
        llm: LLMProvider,
        bus: Optional[EventBus] = None,
        budget: Optional[BudgetTracker] = None,
        taint_tracker: Optional[TaintTracker] = None,
        approval_gate: Optional[ApprovalGate] = None,
        storage: Optional[Any] = None,
    ) -> BaseTopology:
        """Resolve and instantiate the multi-agent topology from SwarmSpec."""
        spec.validate(self.tool_registry)

        topo_cls = self.topology_registry.get(spec.topology)
        if not topo_cls:
            raise SwarmSpecValidationError(
                f"Unknown topology '{spec.topology}'. Available: {self.topology_registry.list_topologies()}"
            )

        active_budget = budget or BudgetTracker(config=spec.budgets, bus=bus, tenant_id=spec.tenant_id)
        active_taint = taint_tracker or TaintTracker()

        return topo_cls(
            tenant_id=spec.tenant_id,
            roles=spec.roles,
            llm=llm,
            budget=active_budget,
            bus=bus,
            taint_tracker=active_taint,
            storage=storage,
            directive=spec.directive,
            approval_gate=approval_gate,
        )


GLOBAL_AGENT_FACTORY = AgentFactory()
GLOBAL_SWARM_FACTORY = SwarmFactory()
