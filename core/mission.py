"""
core/mission.py - The default mission pipeline, used by the Web UI, the CLI, and the API.

Plan -> build topology -> execute under approval and budget controls -> checkpoint each stage.
The swarm's shape comes from the task unless the caller chooses a topology manually.
"""

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.approval import GLOBAL_APPROVAL_GATE, ApprovalGate
from core.budget import BudgetTracker
from core.bus import EventBus
from core.checkpoint import CheckpointStore, MissionCheckpoint
from core.config import CONFIG
from core.factory import GLOBAL_SWARM_FACTORY, MAX_SPEC_STEPS, MAX_SPEC_TOKENS, SwarmFactory, SwarmSpec
from core.llm import LLMProvider
from core.meta_planner import TaskDrivenMetaPlanner
from core.storage import StorageBackend, validate_tenant_id

logger = logging.getLogger("neural_swarm.mission")

# Minimum (steps, tokens) so a plausible plan is not cut off mid-mission.
COMPLEXITY_BUDGET_FLOORS: Dict[str, Tuple[int, int]] = {
    "simple": (8, 30_000),
    "moderate": (32, 120_000),
    "complex": (64, 300_000),
}


@dataclass
class MissionExecutionResult:
    """End-to-end outcome of a mission."""
    mission_id: str
    tenant_id: str
    status: str
    spec: Dict[str, Any]
    output: str
    checkpoints_count: int
    latest_checkpoint_hash: Optional[str]
    audit_events: List[Dict[str, Any]] = field(default_factory=list)


class _MissionLog:
    """Writes signed checkpoints for one mission and announces each on the bus."""

    def __init__(self, store: CheckpointStore, bus: EventBus, mission_id: str, tenant_id: str) -> None:
        self.store = store
        self.bus = bus
        self.mission_id = mission_id
        self.tenant_id = tenant_id
        self.step = 0
        self.stage = 0
        self.topology = "undecided"
        self.last_signature: Optional[str] = None

    def save(self, status: str, output: str, agent_name: str = "system") -> None:
        self.step += 1
        checkpoint = MissionCheckpoint(
            mission_id=self.mission_id,
            tenant_id=self.tenant_id,
            step_number=self.step,
            status=status,
            topology=self.topology,
            stage_index=self.stage,
            accumulated_output=output,
            timestamp=time.time(),
        )
        self.store.save_checkpoint(checkpoint)
        self.last_signature = checkpoint.hmac_signature
        self.stage += 1
        self.bus.emit(
            "checkpoint_saved",
            agent_name=agent_name,
            tenant_id=self.tenant_id,
            mission_id=self.mission_id,
            step_number=checkpoint.step_number,
            status=checkpoint.status,
            hmac_signature=checkpoint.hmac_signature,
        )


class IntegratedMissionRunner:
    """Runs a mission from a one-sentence goal to a checkpointed result."""

    def __init__(
        self,
        llm: LLMProvider,
        checkpoint_store: Optional[CheckpointStore] = None,
        approval_gate: Optional[ApprovalGate] = None,
        swarm_factory: Optional[SwarmFactory] = None,
        meta_planner: Optional[TaskDrivenMetaPlanner] = None,
        bus: Optional[EventBus] = None,
        storage: Optional[StorageBackend] = None,
    ) -> None:
        self.llm = llm
        self.bus = bus or EventBus()
        self.checkpoint_store = checkpoint_store or CheckpointStore(db_path=str(CONFIG.db_path))
        self.approval_gate = approval_gate or GLOBAL_APPROVAL_GATE
        self.swarm_factory = swarm_factory or GLOBAL_SWARM_FACTORY
        self.meta_planner = meta_planner or TaskDrivenMetaPlanner(llm=self.llm)
        self.storage = storage

    def _manual_topology(self, topology_override: Optional[str]) -> Optional[str]:
        """Normalize a manual choice. Empty and 'auto' mean the task decides."""
        choice = (topology_override or "").lower().strip()
        if choice in ("", "auto"):
            return None
        available = self.swarm_factory.topology_registry.list_topologies()
        if choice not in available:
            raise ValueError(f"Unknown topology '{choice}'. Available: {available}")
        return choice

    @staticmethod
    def _apply_budget_policy(spec: SwarmSpec) -> None:
        """Raise budgets to the floor for the task's complexity, and clamp to hard ceilings."""
        floor_steps, floor_tokens = COMPLEXITY_BUDGET_FLOORS.get(
            str(spec.task_analysis.get("complexity", "")), (0, 0)
        )
        budgets = spec.budgets
        budgets.max_swarm_steps = max(
            min(max(budgets.max_swarm_steps, floor_steps), MAX_SPEC_STEPS), CONFIG.mission_min_steps
        )
        budgets.max_total_tokens = max(
            min(max(budgets.max_total_tokens, floor_tokens), MAX_SPEC_TOKENS), CONFIG.mission_min_tokens
        )
        # Run time depends on the deployment's hardware, which the planner can't know.
        budgets.max_wall_seconds = CONFIG.max_wall_seconds

    def plan(
        self,
        prompt: str,
        tenant_id: str = "default",
        topology_override: Optional[str] = None,
        allow_self_heal: bool = False,
        mission_id: Optional[str] = None,
    ) -> SwarmSpec:
        """Design the swarm without running it, so a caller can preview or edit the plan."""
        clean_tenant = validate_tenant_id(tenant_id)
        manual = self._manual_topology(topology_override)
        spec = self.meta_planner.plan_swarm(
            task_prompt=prompt,
            tenant_id=clean_tenant,
            swarm_id=f"swarm_{mission_id}" if mission_id else None,
            required_topology=manual,
            allow_self_heal=allow_self_heal,
        )
        self._apply_budget_policy(spec)
        return spec

    def _spec_from_preview(
        self,
        preset: Dict[str, Any],
        tenant_id: str,
        mission_id: str,
        manual: Optional[str],
        allow_self_heal: bool,
    ) -> SwarmSpec:
        """Re-validate an edited plan: a preview is client data and earns no extra privilege."""
        planner = self.meta_planner
        required = self._manual_topology(manual or str(preset.get("topology") or ""))
        allowed = planner.allowed_capabilities(allow_self_heal)
        try:
            valid, reason = planner.validator.validate_spec(preset, required, allowed)
            spec = planner.build_spec(preset, tenant_id, f"swarm_{mission_id}", required) if valid else None
        except (AttributeError, KeyError, TypeError) as exc:
            raise ValueError(f"Invalid plan: {exc}") from exc
        if spec is None:
            raise ValueError(reason)
        spec.topology_source = "manual" if manual else "preview"
        self._apply_budget_policy(spec)
        return spec

    def _resolve_spec(
        self,
        prompt: str,
        tenant_id: str,
        mission_id: str,
        topology_override: Optional[str],
        allow_self_heal: bool,
        preset_spec: Optional[Dict[str, Any]],
    ) -> SwarmSpec:
        if preset_spec is not None:
            manual = self._manual_topology(topology_override)
            return self._spec_from_preview(preset_spec, tenant_id, mission_id, manual, allow_self_heal)
        return self.plan(prompt, tenant_id, topology_override, allow_self_heal, mission_id)

    def _preflight_clearance(self, prompt: str, tenant_id: str, mission_id: str, approver: str) -> Dict[str, Any]:
        """Record an audited blanket approval for an operator-authorized unattended run."""
        args = {"prompt": prompt, "mission_id": mission_id}
        action = "execute_mission_subtasks"
        rec = self.approval_gate.request_approval(tenant_id, "swarm_lead", action, args)
        self.approval_gate.approve(rec.request_id, approver=approver, reason="Automated pre-flight clearance")
        self.approval_gate.verify_and_burn_token(rec.request_id, action, args, expected_approver=approver)
        return {"request_id": rec.request_id, "status": rec.status, "approver": rec.approver}

    def _announce_plan(self, log: _MissionLog, spec: SwarmSpec) -> None:
        self.bus.emit(
            "mission_planned",
            agent_name="meta_planner",
            tenant_id=log.tenant_id,
            mission_id=log.mission_id,
            topology=spec.topology,
            topology_source=spec.topology_source,
            task_analysis=spec.task_analysis,
            title=spec.title,
            directive=spec.directive,
            roles=[r.to_dict() for r in spec.roles],
        )

    def _stage_listener(self, log: _MissionLog, audit: List[Dict[str, Any]]) -> Any:
        """Checkpoint each finishing specialist and collect approval decisions."""

        def on_event(event: Any) -> None:
            # The server shares one bus, so another tenant's mission can run alongside this one.
            if event.tenant_id != log.tenant_id:
                return
            data = getattr(event, "data", {}) or {}
            if event.event_type == "agent_finish" and event.agent_name not in ("coordinator", "orchestrator", "system"):
                answer = str(data.get("answer", ""))[:1000]
                log.save(f"STAGE_{event.agent_name.upper()}", f"[{event.agent_name}]: {answer}", event.agent_name)
            elif event.event_type == "approval_resolved":
                audit.append({key: data.get(key) for key in ("request_id", "tool", "status", "approver")})

        return on_event

    def _run_topology(self, spec: SwarmSpec, prompt: str, log: _MissionLog, gate: Optional[ApprovalGate], audit: List[Dict[str, Any]]) -> str:
        budget = BudgetTracker(config=spec.budgets, bus=self.bus, tenant_id=log.tenant_id)
        topology = self.swarm_factory.build_topology(
            spec=spec,
            llm=self.llm,
            bus=self.bus,
            budget=budget,
            approval_gate=gate,
            storage=self.storage,
        )
        listener = self._stage_listener(log, audit)
        self.bus.subscribe(listener)
        try:
            return topology.execute(prompt)
        except Exception as exc:
            log.save("FAILED", f"{type(exc).__name__}: {exc}"[:1000])
            self.bus.emit(
                "mission_failed",
                agent_name="system",
                tenant_id=log.tenant_id,
                mission_id=log.mission_id,
                error=str(exc)[:400],
            )
            raise
        finally:
            if listener in self.bus._subscribers:
                self.bus._subscribers.remove(listener)

    def execute(
        self,
        prompt: str,
        tenant_id: str = "default",
        mission_id: Optional[str] = None,
        auto_approve_high_risk: bool = False,
        approver: str = "system-auto-reviewer",
        topology_override: Optional[str] = None,
        allow_self_heal: bool = False,
        preset_spec: Optional[Dict[str, Any]] = None,
    ) -> MissionExecutionResult:
        """Plan (or accept a previewed plan), run it, and return a checkpointed result."""
        clean_tenant = validate_tenant_id(tenant_id)
        self._manual_topology(topology_override)
        effective_id = mission_id or f"m_{clean_tenant}_{secrets.token_hex(6)}"

        log = _MissionLog(self.checkpoint_store, self.bus, effective_id, clean_tenant)
        log.save("INITIALIZING", f"Task: {prompt}")
        self.bus.emit("mission_start", agent_name="system", tenant_id=clean_tenant, mission_id=effective_id, prompt=prompt)

        try:
            spec = self._resolve_spec(prompt, clean_tenant, effective_id, topology_override, allow_self_heal, preset_spec)
        except Exception as exc:
            log.save("FAILED", f"Planning failed: {type(exc).__name__}: {exc}"[:1000])
            raise

        log.topology = spec.topology
        log.save("PLANNED", f"Topology: {spec.topology} ({spec.topology_source}), Roles: {[r.name for r in spec.roles]}", "meta_planner")
        self._announce_plan(log, spec)

        audit: List[Dict[str, Any]] = []
        gate: Optional[ApprovalGate] = self.approval_gate
        if auto_approve_high_risk:
            audit.append(self._preflight_clearance(prompt, clean_tenant, effective_id, approver))
            gate = None

        output = self._run_topology(spec, prompt, log, gate, audit)
        log.save("COMPLETED", output)
        self.bus.emit("mission_completed", agent_name="system", tenant_id=clean_tenant, mission_id=effective_id, output=output[:400])

        return MissionExecutionResult(
            mission_id=effective_id,
            tenant_id=clean_tenant,
            status="COMPLETED",
            spec={
                "swarm_id": spec.swarm_id,
                "title": spec.title,
                "topology": spec.topology,
                "topology_source": spec.topology_source,
                "task_analysis": spec.task_analysis,
                "roles": [r.name for r in spec.roles],
            },
            output=output,
            checkpoints_count=log.step,
            latest_checkpoint_hash=log.last_signature,
            audit_events=audit,
        )
