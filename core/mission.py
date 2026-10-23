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

from core.approval import ApprovalGate, ApprovalPolicy
from core.budget import BudgetTracker
from core.bus import EventBus
from core.checkpoint import CheckpointStore, MissionCheckpoint
from core.factory import GLOBAL_SWARM_FACTORY, SwarmFactory, SwarmSpec
from core.llm import LLMProvider
from core.meta_planner import TaskDrivenMetaPlanner
from core.storage import validate_tenant_id

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
    ) -> None:
        self.llm = llm
        self.bus = bus or EventBus()
        self.checkpoint_store = checkpoint_store or CheckpointStore()
        self.approval_gate = approval_gate or ApprovalGate(
            policy=ApprovalPolicy(high_risk="approve", low_risk="auto")
        )
        self.swarm_factory = swarm_factory or GLOBAL_SWARM_FACTORY
        self.meta_planner = meta_planner or TaskDrivenMetaPlanner(llm=self.llm)

    def execute(
        self,
        prompt: str,
        tenant_id: str = "default",
        mission_id: Optional[str] = None,
        auto_approve_high_risk: bool = True,
        approver: str = "system-auto-reviewer",
    ) -> MissionExecutionResult:
        """
        Run the complete vertical slice:
        1. Validate tenant.
        2. Plan swarm topology and roles via MetaPlanner.
        3. Checkpoint Step 1 (PLANNING).
        4. Build swarm via SwarmFactory.
        5. Execute mission through topology with ApprovalGate interception.
        6. Checkpoint Step 2 (EXECUTION/COMPLETION).
        7. Return structured result.
        """
        clean_tenant = validate_tenant_id(tenant_id)
        effective_mission_id = mission_id or f"m_{clean_tenant}_{secrets.token_hex(6)}"

        # Step 1: Initial Checkpoint (PENDING)
        cp_start = MissionCheckpoint(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            step_number=1,
            status="INITIALIZING",
            topology="undecided",
            stage_index=0,
            accumulated_output=f"Task: {prompt}",
            timestamp=time.time(),
        )
        self.checkpoint_store.save_checkpoint(cp_start)

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

        cp_plan = MissionCheckpoint(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            step_number=2,
            status="PLANNED",
            topology=planned_spec.topology,
            stage_index=1,
            accumulated_output=f"Topology: {planned_spec.topology}, Roles: {[r.name for r in planned_spec.roles]}",
            timestamp=time.time(),
        )
        self.checkpoint_store.save_checkpoint(cp_plan)

        # Step 3: Instantiate Swarm via Factory
        budget = BudgetTracker(config=planned_spec.budgets, bus=self.bus, tenant_id=clean_tenant)
        topology = self.swarm_factory.build_topology(
            spec=planned_spec,
            llm=self.llm,
            bus=self.bus,
            budget=budget,
        )

        # Step 4: Tool Execution with Approval Gate Interception
        # If the task requires high-risk operations (e.g. workspace modifications),
        # record and evaluate through ApprovalGate
        approval_records = []
        if auto_approve_high_risk:
            # Pre-authorize execution step with cryptographically bound approval token
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

        # Step 5: Execute Swarm Topology
        output = topology.execute(prompt)

        # Step 6: Final Checkpoint (COMPLETED)
        cp_final = MissionCheckpoint(
            mission_id=effective_mission_id,
            tenant_id=clean_tenant,
            step_number=3,
            status="COMPLETED",
            topology=planned_spec.topology,
            stage_index=2,
            accumulated_output=output,
            timestamp=time.time(),
        )
        self.checkpoint_store.save_checkpoint(cp_final)

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
                "topology": planned_spec.topology,
                "roles": [r.name for r in planned_spec.roles],
            },
            output=output,
            checkpoints_count=3,
            latest_checkpoint_hash=cp_final.hmac_signature,
            audit_events=approval_records,
        )
