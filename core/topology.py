"""
core/topology.py - FP-12: Pluggable Multi-Agent Topology Registry.

Architectural Purpose:
Replaces rigid procedural branching with a pluggable topology architecture.
Topologies define how multi-agent teams communicate and reach consensus:
1. 'orchestrator': Centralized dispatch & synthesis (Star topology).
2. 'peer': Decentralized inbox-directed messaging (Mesh topology).
3. 'pipeline': Linear sequential workflow (Chain topology).
4. 'review_loop': Generator-Critic iterative refinement with convergence checks.

Dynamic Registry enables loading custom topologies without editing core runtime code.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Type

from core.approval import ApprovalGate
from core.budget import BudgetTracker
from core.bus import EventBus
from core.llm import LLMProvider
from core.peer_swarm import PeerSwarm
from core.registry import DynamicRoleSpec, GLOBAL_TOOL_REGISTRY
from core.roles import RoleRegistry, RoleSpec
from core.security import TaintTracker
from core.storage import SQLiteStorage, StorageBackend
from core.swarm import OrchestratorSwarm, WorkerAgent

logger = logging.getLogger("neural_swarm.topology")


class TopologyExecutionError(RuntimeError):
    """Raised when swarm topology execution fails or stalls."""


class BaseTopology(ABC):
    """Abstract interface defining the execution protocol for a multi-agent topology."""

    def __init__(
        self,
        tenant_id: str,
        roles: List[DynamicRoleSpec],
        llm: LLMProvider,
        budget: BudgetTracker,
        bus: Optional[EventBus] = None,
        taint_tracker: Optional[TaintTracker] = None,
        storage: Optional[StorageBackend] = None,
        directive: str = "",
        approval_gate: Optional[ApprovalGate] = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.roles = roles
        self.llm = llm
        self.budget = budget
        self.bus = bus
        self.taint_tracker = taint_tracker or TaintTracker()
        self.storage = storage or SQLiteStorage()
        self.directive = directive
        self.approval_gate = approval_gate

    def _build_mission_role_registry(self, bus: EventBus) -> tuple[RoleRegistry, Optional[List[str]]]:
        """Build an in-memory RoleRegistry populated with the dynamically planned SwarmSpec roles."""
        reg = RoleRegistry()
        if not self.roles:
            return reg, None

        palette = ["#00ff9d", "#38bdf8", "#ffb800", "#a855f7", "#ff3b6b", "#00f0ff"]
        allowed: List[str] = []
        for idx, r_spec in enumerate(self.roles):
            safe_name = reg._clean_name(r_spec.name)
            if safe_name in {"coordinator", "orchestrator", "user", "airlock"}:
                safe_name = f"{safe_name}_specialist"
            eff_tools = r_spec.resolve_effective_tools(GLOBAL_TOOL_REGISTRY)
            caps = list(dict.fromkeys([*r_spec.capabilities, "@web", "@files", "@memory", *eff_tools]))
            role_obj = RoleSpec(
                name=safe_name,
                title=getattr(r_spec, "purpose", "") or safe_name.replace("_", " ").title(),
                instructions=r_spec.instructions,
                capabilities=caps,
                color=palette[idx % len(palette)],
                is_builtin=False,
                max_steps=r_spec.max_steps,
            )
            reg._roles[safe_name] = role_obj
            allowed.append(safe_name)
            bus.emit(
                "role_created",
                agent_name="meta_planner",
                tenant_id=self.tenant_id,
                role=role_obj.to_dict(),
            )
        return reg, allowed

    @abstractmethod
    def execute(self, prompt: str) -> str:
        """Run the multi-agent topology to completion and return synthesized result."""


class OrchestratorTopology(BaseTopology):
    """Star Topology: Central orchestrator delegates to specialized workers."""

    def execute(self, prompt: str) -> str:
        bus = self.bus or EventBus()
        reg, allowed = self._build_mission_role_registry(bus)
        swarm = OrchestratorSwarm(
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=bus,
            role_registry=reg,
            allowed_roles=allowed,
            directive=self.directive,
            taint_tracker=self.taint_tracker,
            approval_gate=self.approval_gate,
        )
        return swarm.run(prompt, tenant_id=self.tenant_id)


class PeerTopology(BaseTopology):
    """Mesh Topology: Asynchronous inbox messaging between peers with direct handoffs."""

    def execute(self, prompt: str) -> str:
        bus = self.bus or EventBus()
        reg, allowed = self._build_mission_role_registry(bus)
        swarm = PeerSwarm(
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=bus,
            role_registry=reg,
            allowed_roles=allowed,
            directive=self.directive,
            taint_tracker=self.taint_tracker,
            approval_gate=self.approval_gate,
        )
        return swarm.run(prompt, tenant_id=self.tenant_id)


class PipelineTopology(BaseTopology):
    """Chain Topology: Sequential pipeline where output of role[i] becomes input to role[i+1]."""

    def execute(self, prompt: str) -> str:
        if not self.roles:
            raise TopologyExecutionError("Pipeline topology requires at least one configured role.")

        current_input = prompt
        if self.bus:
            self.bus.emit(
                "pipeline_start",
                agent_name="system",
                tenant_id=self.tenant_id,
                total_stages=len(self.roles),
            )

        active_bus = self.bus or EventBus()
        for idx, role_spec in enumerate(self.roles, start=1):
            eff_tools = role_spec.resolve_effective_tools(GLOBAL_TOOL_REGISTRY)
            worker = WorkerAgent(
                name=role_spec.name,
                role=getattr(role_spec, "purpose", "") or role_spec.name.title(),
                instructions=role_spec.instructions,
                llm=self.llm,
                storage=self.storage,
                budget=self.budget,
                bus=active_bus,
                tool_names=eff_tools,
                max_steps=role_spec.max_steps,
                taint_tracker=self.taint_tracker,
                approval_gate=self.approval_gate,
            )

            stage_prompt = f"Pipeline Stage {idx}/{len(self.roles)}. Process the following input:\n\n{current_input}"
            output = worker.execute_task(task_prompt=stage_prompt, tenant_id=self.tenant_id)
            current_input = output or f"[Stage {idx} completed with empty output]"

            if self.bus:
                self.bus.emit(
                    "pipeline_stage_complete",
                    agent_name=role_spec.name,
                    tenant_id=self.tenant_id,
                    stage=idx,
                    total_stages=len(self.roles),
                    output_snippet=current_input[:100],
                )

        return current_input


class ReviewLoopTopology(BaseTopology):
    """Generator-Reviewer Closed Loop: Iterative refinement until accepted or step budget hits."""

    def execute(self, prompt: str) -> str:
        if len(self.roles) < 2:
            # Need at least a generator and a reviewer
            gen_role = self.roles[0] if self.roles else DynamicRoleSpec("generator", "Generate draft", "Generate accurate draft")
            rev_role = DynamicRoleSpec("reviewer", "Critique and approve", "Critique draft and return APPROVED if good")
        else:
            gen_role = self.roles[0]
            rev_role = self.roles[1]

        active_bus = self.bus or EventBus()
        gen_tools = gen_role.resolve_effective_tools(GLOBAL_TOOL_REGISTRY)
        rev_tools = rev_role.resolve_effective_tools(GLOBAL_TOOL_REGISTRY)
        generator = WorkerAgent(
            name=gen_role.name,
            role=getattr(gen_role, "purpose", "") or gen_role.name.title(),
            instructions=gen_role.instructions,
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=active_bus,
            tool_names=gen_tools,
            max_steps=gen_role.max_steps,
            taint_tracker=self.taint_tracker,
            approval_gate=self.approval_gate,
        )

        reviewer = WorkerAgent(
            name=rev_role.name,
            role=getattr(rev_role, "purpose", "") or rev_role.name.title(),
            instructions=rev_role.instructions,
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=active_bus,
            tool_names=rev_tools,
            max_steps=rev_role.max_steps,
            taint_tracker=self.taint_tracker,
            approval_gate=self.approval_gate,
        )

        draft = generator.execute_task(task_prompt=prompt, tenant_id=self.tenant_id)
        max_revisions = 3
        revision = 0

        while revision < max_revisions:
            review_prompt = (
                f"Review the following draft for the original request: '{prompt}'.\n\n"
                f"Draft:\n{draft}\n\n"
                "If it meets all requirements, reply with 'APPROVED' on the first line.\n"
                "Otherwise, provide constructive critique and revision instructions."
            )
            critique = reviewer.execute_task(task_prompt=review_prompt, tenant_id=self.tenant_id)
            if critique.strip().startswith("APPROVED"):
                return draft

            # Iterate
            revision += 1
            refine_prompt = (
                f"The reviewer provided this feedback:\n{critique}\n\n"
                f"Please produce an improved version fulfilling: '{prompt}'."
            )
            draft = generator.execute_task(task_prompt=refine_prompt, tenant_id=self.tenant_id)

        return draft


class TopologyRegistry:
    """Registry managing available multi-agent swarm topologies."""

    def __init__(self) -> None:
        self._topologies: Dict[str, Type[BaseTopology]] = {
            "orchestrator": OrchestratorTopology,
            "peer": PeerTopology,
            "pipeline": PipelineTopology,
            "review_loop": ReviewLoopTopology,
        }

    def register(self, name: str, topology_cls: Type[BaseTopology]) -> None:
        self._topologies[name.lower().strip()] = topology_cls

    def get(self, name: str) -> Optional[Type[BaseTopology]]:
        return self._topologies.get(name.lower().strip())

    def list_topologies(self) -> List[str]:
        return list(self._topologies.keys())


GLOBAL_TOPOLOGY_REGISTRY = TopologyRegistry()
