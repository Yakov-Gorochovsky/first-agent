"""
core/peer_swarm.py - Step 3: Decentralized Peer Network with Inboxes (Data-Driven via RoleRegistry).

Unlike `OrchestratorSwarm` (strictly hierarchical Parent -> Child call/return),
`PeerSwarm` equips every agent with an asynchronous FIFO `inbox` queue:
- `coordinator`: Entry point, dynamic specialist recruiter (`spawn_specialist`), & synthesizer
- All specialist roles registered in `RoleRegistry` (e.g., `researcher`, `coder`, `critic`, or custom roles)

Peers communicate directly with one another via `send_peer_message(to_agent, message)`
without needing the coordinator to relay every intermediate hop.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

from core.budget import BudgetExceededError, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider
from core.roles import RoleRegistry, RoleSpec
from core.security import TaintTracker
from core.storage import StorageBackend
from core.swarm import WorkerAgent


@dataclass
class PeerMessage:
    sender: str
    recipient: str
    content: str
    timestamp: float = field(default_factory=time.time)


class PeerNode:
    """Wraps a WorkerAgent with a dedicated FIFO message inbox."""

    def __init__(self, agent: WorkerAgent) -> None:
        self.agent = agent
        self.inbox: Deque[PeerMessage] = deque()

    @property
    def name(self) -> str:
        return self.agent.name

    @property
    def role(self) -> str:
        return self.agent.role


class PeerSwarm:
    """Decentralized multi-agent network built dynamically from RoleRegistry."""

    def __init__(
        self,
        llm: LLMProvider,
        storage: StorageBackend,
        budget: BudgetTracker,
        bus: EventBus,
        role_registry: Optional[RoleRegistry] = None,
        allowed_roles: Optional[List[str]] = None,
        directive: str = "",
        extra_tools: Optional[Dict[str, Callable[..., str]]] = None,
        extra_schemas: Optional[List[Dict[str, object]]] = None,
        taint_tracker: Optional[TaintTracker] = None,
    ) -> None:
        self.llm = llm
        self.storage = storage
        self.budget = budget
        self.bus = bus
        self.role_registry = role_registry or RoleRegistry()
        self.allowed_roles = allowed_roles
        self.directive = directive.strip()
        self.extra_tools = extra_tools or {}
        self.extra_schemas = extra_schemas or []
        self.taint_tracker = taint_tracker or TaintTracker()
        self.nodes: Dict[str, PeerNode] = {}
        self._final_answer: Optional[str] = None
        self.refresh_peers()

    def _active_role_specs(self) -> List[RoleSpec]:
        all_specs = self.role_registry.list_roles()
        if not self.allowed_roles:
            return all_specs
        allowed_set = {r.lower().strip() for r in self.allowed_roles}
        filtered = [s for s in all_specs if s.name in allowed_set]
        return filtered or all_specs

    def _inbox_snapshot(self) -> Dict[str, int]:
        return {name: len(node.inbox) for name, node in self.nodes.items()}

    def refresh_peers(self) -> None:
        """Rebuild the peer network dynamically from RoleRegistry while preserving existing inbox queues."""
        old_inboxes = {k: v.inbox for k, v in self.nodes.items()}
        self.nodes.clear()

        specs = self._active_role_specs()
        all_peer_names = ["coordinator"] + [s.name for s in specs]

        roster_summary = "\n".join(
            f"  - '{s.name}' ({s.title}): {s.instructions} [Tools: {', '.join(s.tool_names)}]"
            for s in specs
        )

        def _on_spawn(new_spec: RoleSpec) -> None:
            if self.allowed_roles is not None and new_spec.name not in self.allowed_roles:
                self.allowed_roles.append(new_spec.name)
            self._register_single_specialist(new_spec)
            # Update coordinator's peer communication tools & schemas with the new peer
            updated_peers = ["coordinator"] + [s.name for s in self._active_role_specs()]
            new_comm_tools, new_comm_schemas = self._make_peer_comm_tools("coordinator", updated_peers)
            new_comm_tools["spawn_specialist"] = spawn_fn
            new_comm_tools.update(self.extra_tools)
            new_comm_schemas.append(spawn_schema)
            new_comm_schemas.extend(self.extra_schemas)
            if "coordinator" in self.nodes:
                self.nodes["coordinator"].agent.extra_tools = new_comm_tools
                self.nodes["coordinator"].agent.extra_schemas = new_comm_schemas

            self.bus.emit(
                "role_created",
                agent_name="coordinator",
                tenant_id=self.budget.tenant_id,
                role=new_spec.to_dict(),
            )

        spawn_fn, spawn_schema = self.role_registry.make_spawn_specialist_tool(on_spawn=_on_spawn)
        coord_tools, coord_schemas = self._make_peer_comm_tools("coordinator", all_peer_names)
        coord_tools["spawn_specialist"] = spawn_fn
        coord_tools.update(self.extra_tools)
        coord_schemas.append(spawn_schema)
        coord_schemas.extend(self.extra_schemas)
        self._spawn_schema = spawn_schema

        directive_block = f"\nSWARM MISSION DIRECTIVE:\n{self.directive}\n" if self.directive else ""

        coord_worker = WorkerAgent(
            name="coordinator",
            role="Mission Router & Synthesizer",
            instructions=(
                f"{CONFIG.coordinator_prompt}{directive_block}\n"
                f"ACTIVE PEER SPECIALISTS:\n{roster_summary}\n"
                "IMPORTANT: Always call `send_peer_message` to route tasks to specialists, or `spawn_specialist` to create new specialist roles when needed."
            ),
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=self.bus,
            tool_names=[],
            extra_tools=coord_tools,
            extra_schemas=coord_schemas,
            yield_on_tools=["send_peer_message", "finish_swarm"],
            max_steps=CONFIG.max_coordinator_steps,
            taint_tracker=self.taint_tracker,
        )
        coord_node = PeerNode(coord_worker)
        if "coordinator" in old_inboxes:
            coord_node.inbox = old_inboxes["coordinator"]
        self.nodes["coordinator"] = coord_node

        for spec in specs:
            self._register_single_specialist(spec, old_inboxes.get(spec.name))

    def _register_single_specialist(
        self, spec: RoleSpec, existing_inbox: Optional[Deque[PeerMessage]] = None
    ) -> None:
        all_peer_names = ["coordinator"] + [s.name for s in self._active_role_specs()]
        comm_tools, comm_schemas = self._make_peer_comm_tools(spec.name, all_peer_names)
        if self.extra_tools:
            comm_tools.update(self.extra_tools)
            comm_schemas.extend(self.extra_schemas)
        peer_list_str = ", ".join(p for p in all_peer_names if p != spec.name)
        directive_block = (
            f"\nSWARM CONTEXT (Note: 'coordinator' handles routing/spawning; execute your assigned specialist task directly): {self.directive}\n"
            if self.directive
            else ""
        )

        worker = WorkerAgent(
            name=spec.name,
            role=spec.title,
            instructions=(
                f"{spec.instructions}{directive_block}\n"
                f"You are in a peer network with: [{peer_list_str}].\n"
                "- Use your tools to complete your part of the mission directly.\n"
                "- When ready to hand off, message the next peer using `send_peer_message(to_agent=..., message=...)`.\n"
                "- Return your findings in plain text (they are automatically delivered to 'coordinator' for next-step routing), "
                "or call `finish_swarm(final_answer=...)` if the entire user request is verified and complete."
            ),
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=self.bus,
            tool_names=spec.tool_names,
            extra_tools=comm_tools,
            extra_schemas=comm_schemas,
            yield_on_tools=["send_peer_message", "finish_swarm"],
            max_steps=spec.effective_max_steps,
            taint_tracker=self.taint_tracker,
        )
        node = PeerNode(worker)
        if existing_inbox:
            node.inbox = existing_inbox
        self.nodes[spec.name] = node

    def _make_peer_comm_tools(
        self, sender_name: str, all_peers: List[str]
    ) -> tuple[Dict[str, Callable[..., str]], List[Dict[str, object]]]:
        valid_targets = [p for p in all_peers if p != sender_name]

        def send_peer_message(to_agent: str, message: str) -> str:
            target = to_agent.lower().strip()
            if target not in self.nodes:
                return f"Error: Unknown peer '{to_agent}'. Available peers: {list(self.nodes.keys())}"
            self.nodes[target].inbox.append(
                PeerMessage(sender=sender_name, recipient=target, content=message)
            )
            self.bus.emit(
                "peer_message",
                agent_name=sender_name,
                tenant_id=self.budget.tenant_id,
                from_agent=sender_name,
                to_agent=target,
                message=message,
                inboxes=self._inbox_snapshot(),
            )
            return f"Message delivered to '{target}' inbox."

        def finish_swarm(final_answer: str) -> str:
            self._final_answer = final_answer.strip()
            self.bus.emit(
                "swarm_complete",
                agent_name=sender_name,
                tenant_id=self.budget.tenant_id,
                answer=self._final_answer,
            )
            return "Mission marked complete."

        schemas: List[Dict[str, object]] = [
            {
                "type": "function",
                "function": {
                    "name": "send_peer_message",
                    "description": (
                        f"Send a direct message into another peer agent's inbox. "
                        f"Available peers: {valid_targets}."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "to_agent": {
                                "type": "string",
                                "description": f"Recipient peer name (e.g. {', '.join(valid_targets)}).",
                            },
                            "message": {
                                "type": "string",
                                "description": "Payload, findings, or instructions for the recipient peer.",
                            },
                        },
                        "required": ["to_agent", "message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "finish_swarm",
                    "description": "Deliver the final verified response to the user and stop the peer network.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "final_answer": {
                                "type": "string",
                                "description": "Complete, clear final answer for the user.",
                            }
                        },
                        "required": ["final_answer"],
                    },
                },
            },
        ]
        return {"send_peer_message": send_peer_message, "finish_swarm": finish_swarm}, schemas

    def run(self, user_input: str, tenant_id: str = "default") -> str:
        """Deposit the user's request into coordinator's inbox and run the peer scheduler."""
        self.budget.reset()
        self.budget.tenant_id = tenant_id
        self._final_answer = None
        self.taint_tracker = TaintTracker(mission_id=f"peer_{tenant_id}")
        self.refresh_peers()
        for node in self.nodes.values():
            node.inbox.clear()

        self.nodes["coordinator"].inbox.append(
            PeerMessage(sender="user", recipient="coordinator", content=user_input)
        )
        self.bus.emit(
            "peer_message",
            agent_name="user",
            tenant_id=tenant_id,
            from_agent="user",
            to_agent="coordinator",
            message=user_input,
            inboxes=self._inbox_snapshot(),
        )

        past_turns = self.storage.get_session_turns(tenant_id, limit=8)
        last_output = ""
        peer_outputs: List[str] = []

        continuation_count = 0
        try:
            while self._final_answer is None:
                if self.budget.total_steps >= self.budget.config.max_swarm_steps:
                    if self.budget.config.auto_continue and continuation_count < self.budget.config.max_auto_continuations:
                        continuation_count += 1
                        self.bus.emit(
                            "auto_continue",
                            agent_name="coordinator",
                            tenant_id=tenant_id,
                            cycle=continuation_count,
                            max_cycles=self.budget.config.max_auto_continuations,
                            message=f"Auto-continuation cycle {continuation_count}/{self.budget.config.max_auto_continuations} initiated.",
                        )
                        self.budget.reset_turn()
                        if not any(len(n.inbox) > 0 for n in self.nodes.values()):
                            self.nodes["coordinator"].inbox.append(
                                PeerMessage(
                                    sender="system",
                                    recipient="coordinator",
                                    content=(
                                        f"[SYSTEM AUTO-CONTINUATION Cycle {continuation_count}]: Step batch completed. "
                                        "Inspect workspace/ files (SPEC.md, TASKS.json, test execution results). "
                                        "Continue dispatching tasks to pending specialists (coder, test_runner, critic) "
                                        "until all software requirements and tests are 100% complete and verified, "
                                        "then call finish_swarm with the final summary."
                                    ),
                                )
                            )
                        continue

                    combined = "\n\n".join(peer_outputs) if peer_outputs else last_output
                    self._final_answer = (
                        f"{combined}\n\n"
                        f"[Continuation: Swarm step limit reached after {continuation_count} auto-continuation cycles. "
                        f"Reply 'continue' to resume from these findings.]"
                    ).strip()
                    break

                active_node: Optional[PeerNode] = None
                # Schedule specialist peers before coordinator so multi-peer fan-outs
                # complete all specialist work before coordinator synthesizes the batch.
                for name, node in self.nodes.items():
                    if name != "coordinator" and node.inbox:
                        active_node = node
                        break
                if active_node is None and self.nodes["coordinator"].inbox:
                    active_node = self.nodes["coordinator"]

                if active_node is None:
                    break

                batch: List[PeerMessage] = []
                while active_node.inbox:
                    batch.append(active_node.inbox.popleft())

                inbox_prompt = "\n\n".join(
                    m.content if m.sender == "user" else f"[INBOX MESSAGE from '{m.sender}']: {m.content}"
                    for m in batch
                )
                self.bus.emit(
                    "inbox_pickup",
                    agent_name=active_node.name,
                    tenant_id=tenant_id,
                    messages=[{"from": m.sender, "content": m.content} for m in batch],
                    inboxes=self._inbox_snapshot(),
                )

                history = past_turns if active_node.name == "coordinator" else None
                last_output = active_node.agent.execute_task(
                    inbox_prompt,
                    tenant_id=tenant_id,
                    conversation_history=history,
                )

                if self._final_answer is not None:
                    break

                any_pending = any(len(n.inbox) > 0 for n in self.nodes.values())
                if active_node.name == "coordinator":
                    if not any_pending and last_output:
                        self._final_answer = last_output
                        break
                elif last_output:
                    peer_outputs.append(f"[{active_node.name}]: {last_output}")
                    is_simple_single_specialist = (len(self.nodes) <= 2 and not self.extra_tools)
                    if not any_pending and is_simple_single_specialist:
                        # Single-specialist fast-path for standard 1-worker benchmark swarms
                        self.bus.emit(
                            "peer_message",
                            agent_name=active_node.name,
                            tenant_id=tenant_id,
                            from_agent=active_node.name,
                            to_agent="coordinator",
                            message=last_output,
                            inboxes=self._inbox_snapshot(),
                        )
                        self._final_answer = last_output
                        break
                    else:
                        # Multi-specialist team, pipeline, fan-out, or cross-swarm bridge: route output to coordinator inbox
                        self.nodes["coordinator"].inbox.append(
                            PeerMessage(
                                sender=active_node.name,
                                recipient="coordinator",
                                content=last_output,
                            )
                        )
                        self.bus.emit(
                            "peer_message",
                            agent_name=active_node.name,
                            tenant_id=tenant_id,
                            from_agent=active_node.name,
                            to_agent="coordinator",
                            message=last_output,
                            inboxes=self._inbox_snapshot(),
                        )

            final = self._final_answer or ("\n\n".join(peer_outputs) if peer_outputs else last_output) or "Swarm completed with no output."
            self.storage.append_session_turn(tenant_id, "user", user_input)
            self.storage.append_session_turn(tenant_id, "assistant", final)
            return final

        except BudgetExceededError as exc:
            combined = "\n\n".join(peer_outputs) if peer_outputs else last_output
            msg = (
                f"{combined}\n\n[Continuation: {exc} Reply 'continue' to resume from these findings.]"
                if combined
                else f"[Continuation: {exc} Reply 'continue' to resume.]"
            )
            self.storage.append_session_turn(tenant_id, "user", user_input)
            self.storage.append_session_turn(tenant_id, "assistant", msg)
            self.bus.emit(
                "agent_finish",
                agent_name="coordinator",
                tenant_id=tenant_id,
                answer=msg,
            )
            return msg
