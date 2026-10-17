"""
core/peer_swarm.py - Step 3: Decentralized Peer Network with Inboxes.

Unlike `OrchestratorSwarm` (strictly hierarchical Parent -> Child call/return),
`PeerSwarm` equips every agent with an asynchronous FIFO `inbox` queue:
- `coordinator`: Entry point & mission router
- `researcher`: Live web search, URL fetching, weather, time
- `coder`: Python 3 execution sandbox, math calculator, workspace files
- `critic`: QA verifier & security/accuracy reviewer

Peers communicate directly with one another via `send_peer_message(to_agent, message)`
(e.g., `researcher -> coder -> critic -> coordinator`) without needing the coordinator
to relay every intermediate hop.
"""

from collections import deque
from dataclasses import dataclass, field
import time
from typing import Callable, Deque, Dict, List, Optional
from core.budget import BudgetExceededError, BudgetTracker
from core.bus import EventBus
from core.llm import LLMProvider
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
    """Decentralized multi-agent network where agents message each other's inboxes directly."""

    def __init__(
        self,
        llm: LLMProvider,
        storage: StorageBackend,
        budget: BudgetTracker,
        bus: EventBus,
    ) -> None:
        self.llm = llm
        self.storage = storage
        self.budget = budget
        self.bus = bus
        self.nodes: Dict[str, PeerNode] = {}
        self._final_answer: Optional[str] = None
        self._setup_peers()

    def _inbox_snapshot(self) -> Dict[str, int]:
        return {name: len(node.inbox) for name, node in self.nodes.items()}

    def _setup_peers(self) -> None:
        peer_specs = [
            (
                "coordinator",
                "Mission Router & Synthesizer",
                (
                    "You are 'coordinator' in a peer-to-peer agent network ('coordinator', 'researcher', 'coder', 'critic').\n"
                    "- For simple memory/greeting tasks, handle them directly and call `finish_swarm`.\n"
                    "- For web/weather/time tasks, use `send_peer_message` to 'researcher'.\n"
                    "- For Python coding/math/file tasks, use `send_peer_message` to 'coder'.\n"
                    "- When a peer sends you verified results in your inbox, call `finish_swarm(final_answer=...)`."
                ),
                ["remember_fact", "recall_memories"],
            ),
            (
                "researcher",
                "Web & Live Data Peer",
                (
                    "You are 'researcher' in a peer agent network.\n"
                    "Use your tools (`web_search`, `fetch_url`, `get_weather`, `get_current_time`) to gather data.\n"
                    "- If calculation or Python processing is needed next, call `send_peer_message(to_agent='coder', message=...)` directly!\n"
                    "- Otherwise, send your findings to 'critic' or 'coordinator' via `send_peer_message`."
                ),
                ["web_search", "fetch_url", "get_weather", "get_current_time"],
            ),
            (
                "coder",
                "Python Sandbox & Math Peer",
                (
                    "You are 'coder' in a peer agent network.\n"
                    "Use `run_python`, `calculator`, or `save_note`/`read_note`/`list_notes` to execute tasks.\n"
                    "Once your code or math is executed, send the output via `send_peer_message` to 'critic' for review "
                    "(or to 'coordinator' if ready)."
                ),
                ["run_python", "calculator", "save_note", "read_note", "list_notes"],
            ),
            (
                "critic",
                "QA & Verification Peer",
                (
                    "You are 'critic' in a peer agent network.\n"
                    "Review results sent to your inbox for accuracy and completeness.\n"
                    "- If there is an error or missing requirement, call `send_peer_message` back to 'coder' or 'researcher' to fix it.\n"
                    "- If the result is accurate and complete, call `finish_swarm(final_answer=...)` directly to complete the mission!"
                ),
                ["calculator", "read_note", "list_notes"],
            ),
        ]

        peer_names = [s[0] for s in peer_specs]

        for name, role, instructions, tools in peer_specs:
            extra_tools, extra_schemas = self._make_peer_comm_tools(name, peer_names)
            worker = WorkerAgent(
                name=name,
                role=role,
                instructions=instructions,
                llm=self.llm,
                storage=self.storage,
                budget=self.budget,
                bus=self.bus,
                tool_names=tools,
                extra_tools=extra_tools,
                extra_schemas=extra_schemas,
                yield_on_tools=["send_peer_message", "finish_swarm"],
            )
            self.nodes[name] = PeerNode(worker)

    def _make_peer_comm_tools(
        self, sender_name: str, all_peers: List[str]
    ) -> tuple[Dict[str, Callable[..., str]], List[Dict[str, object]]]:
        valid_targets = [p for p in all_peers if p != sender_name]

        def send_peer_message(to_agent: str, message: str) -> str:
            target = to_agent.lower().strip()
            if target not in self.nodes:
                return f"Error: Unknown peer '{to_agent}'. Valid peers: {valid_targets}"
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
                                "enum": valid_targets,
                                "description": "Recipient peer agent name.",
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
        for node in self.nodes.values():
            node.inbox.clear()

        # Seed initial message from User -> Coordinator's inbox
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

        try:
            while self._final_answer is None:
                # Find the next peer node with pending inbox messages
                active_node: Optional[PeerNode] = None
                for node in self.nodes.values():
                    if node.inbox:
                        active_node = node
                        break

                if active_node is None:
                    break

                # Drain all queued messages currently in this peer's inbox
                batch: List[PeerMessage] = []
                while active_node.inbox:
                    batch.append(active_node.inbox.popleft())

                inbox_prompt = "\n\n".join(
                    f"[INBOX MESSAGE from '{m.sender}']: {m.content}" for m in batch
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

                # If the active peer finished its turn without sending any peer message
                # or calling finish_swarm, route its plain-text output:
                any_pending = any(len(n.inbox) > 0 for n in self.nodes.values())
                if not any_pending and last_output:
                    if active_node.name == "coordinator":
                        self._final_answer = last_output
                        break
                    else:
                        # Auto-deliver worker reply to coordinator's inbox
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

            final = self._final_answer or last_output or "Swarm completed with no output."
            self.storage.append_session_turn(tenant_id, "user", user_input)
            self.storage.append_session_turn(tenant_id, "assistant", final)
            return final

        except BudgetExceededError as exc:
            msg = f"[Budget Guardrail Triggered] {exc}"
            self.bus.emit(
                "budget_exceeded",
                agent_name="coordinator",
                tenant_id=tenant_id,
                error=str(exc),
            )
            return msg
