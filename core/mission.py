"""
core/mission.py - Integrated Mission Pipeline (Vertical Slice Execution).

Architectural Purpose:
Wires together the five key pillars into an integrated, production-ready execution slice:
1. TaskDrivenMetaPlanner (dynamic task decomposition & SwarmSpec generation)
2. SwarmFactory (dynamic topology & specialist agent instantiation)
3. BaseTopology / Swarm Runtime (multi-agent execution loop)
4. ApprovalGate (cryptographic approval tokens & least-privilege action authorization)
5. CheckpointStore & DurableMissionRunner (HMAC-SHA256 signed durable state persistence)
"""

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.approval import GLOBAL_APPROVAL_GATE, ApprovalGate, ApprovalPolicy
from core.budget import BudgetTracker
from core.bus import EventBus
from core.checkpoint import CheckpointStore, MissionCheckpoint
from core.config import CONFIG
from core.factory import GLOBAL_SWARM_FACTORY, SwarmFactory, SwarmSpec
from core.llm import LLMProvider
from core.meta_planner import TaskDrivenMetaPlanner
from core.storage import StorageBackend, validate_tenant_id

logger = logging.getLogger("neural_swarm.mission")


@dataclass
class MissionExecutionResult:
    """End-to-end outcome of a vertically-integrated swarm mission."""
    mission_id: str
    tenant_id: str
    status: str
    spec: Dict[str, Any]
    output: str
    checkpoints_count: int
    latest_checkpoint_hash: Optional[str]
    audit_events: List[Dict[str, Any]] = field(default_factory=list)


class IntegratedMissionRunner:
    """
    Executes an end-to-end mission slice:
    Planner -> SwarmFactory -> Swarm Execution -> Approval Gate -> Checkpoint Store.
    """

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

    def execute(
        self,
        prompt: str,
        tenant_id: str = "default",
        mission_id: Optional[str] = None,
        auto_approve_high_risk: bool = False,
        approver: str = "system-auto-reviewer",
        topology_override: Optional[str] = None,
    ) -> MissionExecutionResult:
        """
        Run the complete vertical slice:
        1. Validate tenant.
        2. Plan swarm topology and roles via MetaPlanner.
        3. Checkpoint Step 1 (INITIALIZING) and Step 2 (PLANNED).
        4. Build swarm via SwarmFactory with live ApprovalGate interception.
        5. Execute mission through topology, persisting HMAC-signed checkpoints per specialist stage.
        6. Checkpoint Final Completion (COMPLETED).
        7. Return structured result.
        """
        clean_tenant = validate_tenant_id(tenant_id)
        effective_mission_id = mission_id or f"m_{clean_tenant}_{secrets.token_hex(6)}"
        step_counter = 1

        # Step 1: Initial Checkpoint (INITIALIZING)
        cp_start = MissionCheckpoint(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            step_number=step_counter,
            status="INITIALIZING",
            topology="undecided",
            stage_index=0,
            accumulated_output=f"Task: {prompt}",
            timestamp=time.time(),
        )
        self.checkpoint_store.save_checkpoint(cp_start)
        self.bus.emit(
            "checkpoint_saved",
            agent_name="system",
            tenant_id=clean_tenant,
            mission_id=effective_mission_id,
            step_number=cp_start.step_number,
            status=cp_start.status,
            hmac_signature=cp_start.hmac_signature,
        )

        self.bus.emit(
            "mission_start",
            agent_name="system",
            tenant_id=clean_tenant,
            mission_id=effective_mission_id,
            prompt=prompt,
        )

        # Step 2: Meta-Planning
        planned_spec: SwarmSpec = self.meta_planner.plan_swarm(
            task_prompt=prompt,
            tenant_id=clean_tenant,
            swarm_id=f"swarm_{effective_mission_id}",
        )
        if topology_override and topology_override.lower().strip() not in ("", "auto"):
            planned_spec.topology = topology_override.lower().strip()

        step_counter += 1
        cp_plan = MissionCheckpoint(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            step_number=step_counter,
            status="PLANNED",
            topology=planned_spec.topology,
            stage_index=1,
            accumulated_output=f"Topology: {planned_spec.topology}, Roles: {[r.name for r in planned_spec.roles]}",
            timestamp=time.time(),
        )
        self.checkpoint_store.save_checkpoint(cp_plan)
        self.bus.emit(
            "checkpoint_saved",
            agent_name="meta_planner",
            tenant_id=clean_tenant,
            mission_id=effective_mission_id,
            step_number=cp_plan.step_number,
            status=cp_plan.status,
            hmac_signature=cp_plan.hmac_signature,
        )
        self.bus.emit(
            "mission_planned",
            agent_name="meta_planner",
            tenant_id=clean_tenant,
            mission_id=effective_mission_id,
            topology=planned_spec.topology,
            title=planned_spec.title,
            directive=planned_spec.directive,
            roles=[r.to_dict() for r in planned_spec.roles],
        )

        # Step 3: Approval Gate Configuration
        approval_records: List[Dict[str, Any]] = []
        active_gate: Optional[ApprovalGate] = self.approval_gate
        if auto_approve_high_risk:
            rec = self.approval_gate.request_approval(
                tenant_id=clean_tenant,
                agent_name="swarm_lead",
                action="execute_mission_subtasks",
                args={"prompt": prompt, "mission_id": effective_mission_id},
            )
            self.approval_gate.approve(rec.request_id, approver=approver, reason="Automated pre-flight clearance")
            self.approval_gate.verify_and_burn_token(
                request_id=rec.request_id,
                expected_action="execute_mission_subtasks",
                submitted_args={"prompt": prompt, "mission_id": effective_mission_id},
                expected_approver=approver,
            )
            approval_records.append({
                "request_id": rec.request_id,
                "status": rec.status,
                "approver": rec.approver,
            })
            active_gate = None

        # Step 4: Instantiate Swarm via Factory
        budget = BudgetTracker(config=planned_spec.budgets, bus=self.bus, tenant_id=clean_tenant)
        topology = self.swarm_factory.build_topology(
            spec=planned_spec,
            llm=self.llm,
            bus=self.bus,
            budget=budget,
            approval_gate=active_gate,
            storage=self.storage,
        )

        # Subscribe temporary listener for intermediate specialist stage checkpoints
        stage_idx = 1

        def _on_bus_event(event: Any) -> None:
            nonlocal step_counter, stage_idx
            ev_data = getattr(event, "data", {}) or {}
            if event.event_type == "agent_finish" and event.agent_name not in ("coordinator", "orchestrator", "system"):
                step_counter += 1
                stage_idx += 1
                ans_snippet = str(ev_data.get("answer", ""))[:1000]
                cp_stage = MissionCheckpoint(
                    mission_id=effective_mission_id,
                    tenant_id=clean_tenant,
                    step_number=step_counter,
                    status=f"STAGE_{event.agent_name.upper()}",
                    topology=planned_spec.topology,
                    stage_index=stage_idx,
                    accumulated_output=f"[{event.agent_name}]: {ans_snippet}",
                    timestamp=time.time(),
                )
                self.checkpoint_store.save_checkpoint(cp_stage)
                self.bus.emit(
                    "checkpoint_saved",
                    agent_name=event.agent_name,
                    tenant_id=clean_tenant,
                    mission_id=effective_mission_id,
                    step_number=cp_stage.step_number,
                    status=cp_stage.status,
                    hmac_signature=cp_stage.hmac_signature,
                )
            elif event.event_type == "approval_resolved":
                approval_records.append({
                    "request_id": ev_data.get("request_id"),
                    "tool": ev_data.get("tool"),
                    "status": ev_data.get("status"),
                    "approver": ev_data.get("approver"),
                })

        self.bus.subscribe(_on_bus_event)
        try:
            # Step 5: Execute Swarm Topology
            output = topology.execute(prompt)
        finally:
            if _on_bus_event in self.bus._subscribers:
                self.bus._subscribers.remove(_on_bus_event)

        # Step 6: Final Checkpoint (COMPLETED)
        step_counter += 1
        stage_idx += 1
        cp_final = MissionCheckpoint(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            step_number=step_counter,
            status="COMPLETED",
            topology=planned_spec.topology,
            stage_index=stage_idx,
            accumulated_output=output,
            timestamp=time.time(),
        )
        self.checkpoint_store.save_checkpoint(cp_final)
        self.bus.emit(
            "checkpoint_saved",
            agent_name="system",
            tenant_id=clean_tenant,
            mission_id=effective_mission_id,
            step_number=cp_final.step_number,
            status=cp_final.status,
            hmac_signature=cp_final.hmac_signature,
        )

        self.bus.emit(
            "mission_completed",
            agent_name="system",
            tenant_id=clean_tenant,
            mission_id=effective_mission_id,
            output=output[:400],
        )

        return MissionExecutionResult(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            status="COMPLETED",
            spec={
                "swarm_id": planned_spec.swarm_id,
                "title": planned_spec.title,
                "topology": planned_spec.topology,
                "roles": [r.name for r in planned_spec.roles],
            },
            output=output,
            checkpoints_count=step_counter,
            latest_checkpoint_hash=cp_final.hmac_signature,
            audit_events=approval_records,
        )
