"""
frameworks_comparison.py - Step 6: Understanding Agent Frameworks (LangGraph, OpenAI Agents SDK, Claude SDK).

Because you built the raw harness first (Steps 1-5), you can now see *exactly* what
popular frameworks abstract away under the hood.

This file demonstrates—and executes—the two dominant industry framework patterns
built directly on top of your `core/` primitives:

1. Pattern A: LangGraph Style (`StateGraph`, `Nodes`, `Conditional Edges`)
   - What LangGraph abstracts: A shared `state` dictionary passed between node functions,
     and a `conditional_edge` router function that decides the next node name.
   - How it maps to your code: It is a formalization of `PeerSwarm`'s while-loop scheduler!

2. Pattern B: OpenAI Agents SDK Style (`Agent(..., handoffs=[...])`, `Runner.run()`)
   - What OpenAI SDK abstracts: Turning another `Agent` object into a callable tool
     (`transfer_to_coder`) that switches the active agent pointer.
   - How it maps to your code: It is identical to `yield_on_tools=["send_peer_message"]`
     in `core/peer_swarm.py`!
"""

from dataclasses import dataclass, field
import sys
from typing import Any, Callable, Dict, List, Optional

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.bus import EventBus

END = "__END__"


# ===========================================================================
# PATTERN A: LangGraph Style (StateGraph + Nodes + Conditional Edges)
# ===========================================================================
# In real `langgraph`:
#   from langgraph.graph import StateGraph, END
#   graph = StateGraph(AgentState)
#   graph.add_node("researcher", researcher_node)
#   graph.add_conditional_edges("researcher", router_fn)
#   app = graph.compile()
#
# Below is the exact ~45-line engine that `StateGraph.compile()` builds under the hood!
# ===========================================================================

class StateGraph:
    """Equivalent to `langgraph.graph.StateGraph`—shows what LangGraph does in ~45 lines."""

    def __init__(self, bus: Optional[EventBus] = None) -> None:
        self.nodes: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}
        self.edges: Dict[str, str] = {}
        self.conditional_edges: Dict[str, Callable[[Dict[str, Any]], str]] = {}
        self.entry_point: Optional[str] = None
        self.bus = bus

    def add_node(self, name: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self.nodes[name] = fn

    def set_entry_point(self, name: str) -> None:
        self.entry_point = name

    def add_edge(self, from_node: str, to_node: str) -> None:
        self.edges[from_node] = to_node

    def add_conditional_edges(
        self,
        from_node: str,
        router_fn: Callable[[Dict[str, Any]], str],
    ) -> None:
        self.conditional_edges[from_node] = router_fn

    def invoke(self, initial_state: Dict[str, Any], max_steps: int = 10) -> Dict[str, Any]:
        """Run the state machine graph until it reaches END or hits max_steps."""
        state = dict(initial_state)
        current = self.entry_point
        steps = 0

        while current and current != END:
            if steps >= max_steps:
                raise RuntimeError(f"StateGraph exceeded max_steps ({max_steps})")
            node_fn = self.nodes.get(current)
            if not node_fn:
                raise ValueError(f"Unknown graph node: {current}")

            if self.bus:
                self.bus.emit("agent_start", agent_name=current, tenant_id="langgraph_demo")

            # LangGraph merges the dict returned by node_fn into the shared state
            updates = node_fn(state)
            if isinstance(updates, dict):
                state.update(updates)

            steps += 1
            if current in self.conditional_edges:
                next_node = self.conditional_edges[current](state)
            else:
                next_node = self.edges.get(current, END)

            if self.bus and next_node != END:
                self.bus.emit(
                    "peer_message",
                    agent_name=current,
                    tenant_id="langgraph_demo",
                    from_agent=current,
                    to_agent=next_node,
                    message=f"State transition: {current} -> {next_node}",
                )
            current = next_node

        return state


# ===========================================================================
# PATTERN B: OpenAI Agents SDK Style (`Agent` with `handoffs` + `Runner.run`)
# ===========================================================================
# In real `openai-agents` SDK:
#   coder = Agent(name="coder", instructions="...", tools=[...])
#   triage = Agent(name="triage", instructions="...", handoffs=[coder])
#   result = Runner.run_sync(triage, "Write a script...")
#
# Below is the exact mechanism OpenAI's `Runner` uses to execute handoffs!
# ===========================================================================

@dataclass
class SDKAgent:
    name: str
    instructions: str
    handler: Callable[[str], tuple[str, Optional["SDKAgent"]]]
    handoffs: List["SDKAgent"] = field(default_factory=list)


class SDKRunner:
    """Equivalent to OpenAI Agents SDK `Runner.run_sync()`—shows how handoffs work."""

    @staticmethod
    def run_sync(
        starting_agent: SDKAgent,
        user_input: str,
        bus: Optional[EventBus] = None,
        max_turns: int = 8,
    ) -> str:
        active_agent = starting_agent
        current_payload = user_input

        for _ in range(max_turns):
            if bus:
                bus.emit("agent_start", agent_name=active_agent.name, tenant_id="openai_sdk_demo")

            output, next_agent = active_agent.handler(current_payload)
            if next_agent is None:
                return output

            if bus:
                bus.emit(
                    "peer_message",
                    agent_name=active_agent.name,
                    tenant_id="openai_sdk_demo",
                    from_agent=active_agent.name,
                    to_agent=next_agent.name,
                    message=output,
                )
            active_agent = next_agent
            current_payload = output

        raise RuntimeError(f"SDKRunner exceeded max_turns ({max_turns})")


# ===========================================================================
# Demo Execution & Mapping Table
# ===========================================================================

def demo_langgraph_pattern() -> Dict[str, Any]:
    """Run a 3-node Coordinator -> Coder -> Critic loop using the LangGraph StateGraph pattern."""
    bus = EventBus()
    graph = StateGraph(bus=bus)

    def coordinator_node(state: Dict[str, Any]) -> Dict[str, Any]:
        return {"plan": f"Compute square of {state['input_num']} and verify."}

    def coder_node(state: Dict[str, Any]) -> Dict[str, Any]:
        n = state["input_num"]
        return {"result": n * n, "attempts": state.get("attempts", 0) + 1}

    def critic_node(state: Dict[str, Any]) -> Dict[str, Any]:
        verified = state["result"] == state["input_num"] ** 2
        return {"verified": verified, "final_answer": f"Verified result: {state['result']}"}

    def critic_router(state: Dict[str, Any]) -> str:
        return END if state.get("verified") else "coder"

    graph.add_node("coordinator", coordinator_node)
    graph.add_node("coder", coder_node)
    graph.add_node("critic", critic_node)

    graph.set_entry_point("coordinator")
    graph.add_edge("coordinator", "coder")
    graph.add_edge("coder", "critic")
    graph.add_conditional_edges("critic", critic_router)

    return graph.invoke({"input_num": 14})


def demo_openai_sdk_pattern() -> str:
    """Run a Triage -> Security Reviewer -> Coder handoff chain using the OpenAI Agents SDK pattern."""
    bus = EventBus()

    coder = SDKAgent(
        name="coder",
        instructions="Fix vulnerable SQL query.",
        handler=lambda msg: ("Fixed with parameterized query: cursor.execute('SELECT * FROM t WHERE id = ?', (uid,))", None),
    )
    security = SDKAgent(
        name="security_reviewer",
        instructions="Flag SQLi and handoff to coder.",
        handler=lambda msg: (f"Found SQLi in '{msg}'. Handing off to coder.", coder),
        handoffs=[coder],
    )
    triage = SDKAgent(
        name="coordinator",
        instructions="Route code snippets to security_reviewer.",
        handler=lambda msg: (msg, security),
        handoffs=[security],
    )

    return SDKRunner.run_sync(triage, "SELECT * FROM t WHERE id = " + "user_input", bus=bus)


def main() -> None:
    print("=" * 78)
    print(" STEP 6: WHAT AGENT FRAMEWORKS ABSTRACT AWAY")
    print("=" * 78)

    lg_state = demo_langgraph_pattern()
    print("\n1. [LangGraph Pattern: StateGraph + Conditional Edges]")
    print(f"   Final State: {lg_state}")

    sdk_out = demo_openai_sdk_pattern()
    print("\n2. [OpenAI Agents SDK Pattern: Agent Handoffs + Runner]")
    print(f"   Final Output: {sdk_out}")

    print("\n" + "-" * 78)
    print(" ARCHITECTURE MAPPING: YOUR CODE vs. FRAMEWORKS")
    print("-" * 78)
    mapping = [
        ("Tool Loop Harness", "WorkerAgent.execute_task()", "LangChain AgentExecutor / Claude SDK"),
        ("Hub & Spoke", "OrchestratorSwarm (Step 2)", "Claude Subagents / CrewAI Hierarchical"),
        ("Peer Inboxes / Graph", "PeerSwarm (Step 3)", "LangGraph StateGraph / AutoGen GroupChat"),
        ("Agent Handoffs", "yield_on_tools=['send_peer_message']", "OpenAI Agents SDK `handoffs=[...]`"),
        ("Checkpointer / Memory", "SQLiteStorage + PrivacyAirlock", "LangGraph SqliteSaver + Store"),
        ("Tracing / Streaming", "EventBus + server.py SSE", "LangSmith / Langfuse / Arize Phoenix"),
    ]
    print(f" {'Concept':<22} | {'Your Custom Code':<34} | {'Framework Equivalent'}")
    print("-" * 78)
    for concept, yours, fw in mapping:
        print(f" {concept:<22} | {yours:<34} | {fw}")
    print("=" * 78)


if __name__ == "__main__":
    main()
