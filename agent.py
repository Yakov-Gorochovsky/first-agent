"""
agent.py - Modular Multi-Tenant AI Swarm CLI (Supports Step 2 Orchestrator & Step 3 Peer Network).

Uses loosely coupled interfaces from `core/`:
- `core/llm.py`        (LLMProvider: Ollama, Hugging Face, Groq, Gemini, OpenAI)
- `core/storage.py`    (StorageBackend: SQLite multi-tenant isolation + PrivacyAirlock global skills)
- `core/budget.py`     (BudgetTracker: step, token, and wall-time caps)
- `core/bus.py`        (EventBus: pub/sub telemetry for CLI and SSE UI)
- `core/roles.py`      (RoleRegistry & Capability Bundles: @web, @code, @files, @memory)
- `core/swarm.py`      (Step 2: OrchestratorSwarm)
- `core/peer_swarm.py` (Step 3: PeerSwarm with decentralized inboxes)
"""

import argparse
import os
from dotenv import load_dotenv

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus, SwarmEvent
from core.llm import OpenAICompatibleProvider
from core.peer_swarm import PeerSwarm
from core.roles import CAPABILITY_BUNDLES, RoleRegistry
from core.storage import SQLiteStorage
from core.swarm import OrchestratorSwarm
from tools import WORKSPACE_DIR

load_dotenv()

API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:11434/v1")
API_KEY = os.getenv("API_KEY", "ollama")
MODEL_NAME = os.getenv("MODEL_NAME", "qwen3:8b")
MAX_STEPS = int(os.getenv("MAX_STEPS", "8"))
VERBOSE = os.getenv("VERBOSE", "true").lower() == "true"
DB_PATH = WORKSPACE_DIR / "swarm.db"


def console_event_listener(event: SwarmEvent) -> None:
    """Subscribe to EventBus to render live swarm telemetry in the terminal."""
    if not VERBOSE:
        return
    etype = event.event_type
    agent = event.agent_name
    data = event.data

    if etype == "delegation":
        print(f"  [Orchestrator -> {data.get('to_worker')}] Task: {data.get('task')}")
    elif etype == "peer_message":
        print(
            f"  [Inbox: {data.get('from_agent')} -> {data.get('to_agent')}] "
            f"{str(data.get('message', ''))[:200]}"
        )
    elif etype == "role_created":
        role = data.get("role", {})
        print(
            f"  [RoleRegistry | New Role Spawned] '{role.get('name')}' ({role.get('title')}) "
            f"Tools: {role.get('resolved_tools')}"
        )
    elif etype == "tool_call":
        print(f"  [{agent} | Tool Call] {data.get('tool')}({data.get('arguments')})")
    elif etype == "tool_result":
        res = str(data.get("result", ""))
        preview = res if len(res) <= 220 else res[:220] + " ... [truncated]"
        print(f"  [{agent} | Observation] {preview}")
    elif etype == "skill_learned":
        print(
            f"  [PrivacyAirlock -> Global Skill] [{data.get('category')}] "
            f"{data.get('problem_pattern')} => {data.get('solution_rule')}"
        )


def build_swarms() -> tuple[OrchestratorSwarm, PeerSwarm, RoleRegistry, SQLiteStorage, BudgetTracker, EventBus]:
    bus = EventBus()
    bus.subscribe(console_event_listener)

    storage = SQLiteStorage(db_path=DB_PATH, max_trace_rows=500)
    role_registry = RoleRegistry()
    budget = BudgetTracker(
        config=BudgetConfig(
            max_swarm_steps=MAX_STEPS * 2,
            max_steps_per_agent=MAX_STEPS,
            max_total_tokens=24000,
            max_wall_seconds=180.0,
        ),
        bus=bus,
    )
    llm = OpenAICompatibleProvider(
        base_url=API_BASE_URL,
        api_key=API_KEY,
        model=MODEL_NAME,
    )
    orch = OrchestratorSwarm(
        llm=llm, storage=storage, budget=budget, bus=bus, role_registry=role_registry
    )
    peer = PeerSwarm(
        llm=llm, storage=storage, budget=budget, bus=bus, role_registry=role_registry
    )
    return orch, peer, role_registry, storage, budget, bus


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Modular AI Agent Swarm.")
    parser.add_argument("-p", "--prompt", type=str, help="Run a single prompt and exit.")
    parser.add_argument(
        "-t",
        "--tenant",
        type=str,
        default="client_a",
        help="Tenant ID for isolated memory (default: client_a).",
    )
    parser.add_argument(
        "-m",
        "--mode",
        type=str,
        choices=["peer", "orchestrator"],
        default="peer",
        help="Swarm topology: 'peer' (Step 3 Inbox Network) or 'orchestrator' (Step 2).",
    )
    parser.add_argument("--new-session", action="store_true", help="Clear tenant session history.")
    args = parser.parse_args()

    orch_swarm, peer_swarm, role_registry, storage, budget, _ = build_swarms()
    tenant_id = args.tenant
    mode = args.mode

    if args.new_session:
        storage.clear_session(tenant_id)

    role_names = ", ".join(r.name for r in role_registry.list_roles())
    print("=" * 78)
    print(f" Modular AI Swarm | Model: {MODEL_NAME} | Tenant: '{tenant_id}' | Mode: {mode.upper()}")
    print(f" Active Roles: [{role_names}] (Managed via core/roles.py & workspace/roles.json)")
    print(" Web UI:       Run `python server.py` and open http://localhost:8000")
    print("=" * 78)

    def run_active(prompt_text: str) -> str:
        if mode == "orchestrator":
            return orch_swarm.run(prompt_text, tenant_id=tenant_id)
        return peer_swarm.run(prompt_text, tenant_id=tenant_id)

    if args.prompt:
        print(f"\nYou ({tenant_id}): {args.prompt}")
        reply = run_active(args.prompt)
        print(f"\nSwarm: {reply}")
        print(f"  [Budget Summary] {budget.summary()}\n")
        return

    print(
        "Commands: '/roles' | '/add-role <name> | <title> | <@bundles> | <instructions>' | "
        "'/del-role <name>' | '/mode peer|orchestrator' | '/tenant <id>' | '/memory' | '/skills' | 'exit'\n"
    )
    while True:
        try:
            user_input = input(f"You ({tenant_id} | {mode}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            print("Goodbye!")
            break
        if user_input.lower() == "/roles":
            print("\nRegistered Specialist Roles:")
            for r in role_registry.list_roles():
                print(f"  - {r.name} ({r.title}) | Bundles: {r.capabilities} -> Tools: {r.tool_names}")
                print(f"    Instructions: {r.instructions}")
            print(f"\nAvailable Capability Bundles: {list(CAPABILITY_BUNDLES.keys())}\n")
            continue
        if user_input.lower().startswith("/add-role "):
            raw = user_input[len("/add-role ") :].strip()
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 3:
                print(
                    "  [Usage] /add-role <name> | <title> | <@web,@code,@files> | <instructions>\n"
                )
                continue
            r_name = parts[0]
            r_title = parts[1]
            r_caps = [c.strip() for c in parts[2].split(",") if c.strip()]
            r_inst = parts[3] if len(parts) > 3 else f"Act as {r_title}."
            spec = role_registry.register_role(r_name, r_title, r_inst, r_caps)
            print(
                f"  [Created Role] '{spec.name}' ({spec.title}) with tools: {spec.tool_names}\n"
            )
            continue
        if user_input.lower().startswith("/del-role "):
            r_name = user_input.split(maxsplit=1)[1].strip()
            if role_registry.delete_role(r_name):
                print(f"  [Deleted Role] '{r_name}'\n")
            else:
                print(f"  [Role '{r_name}' not found.]\n")
            continue
        if user_input.lower().startswith("/mode "):
            new_mode = user_input.split(maxsplit=1)[1].strip().lower()
            if new_mode in {"peer", "orchestrator"}:
                mode = new_mode
                print(f"  [Switched swarm topology to '{mode}'.]\n")
            else:
                print("  [Choose '/mode peer' or '/mode orchestrator'.]\n")
            continue
        if user_input.lower().startswith("/tenant "):
            tenant_id = user_input.split(maxsplit=1)[1].strip()
            print(f"  [Switched active tenant to '{tenant_id}'. Private memory is isolated.]\n")
            continue
        if user_input.lower() == "/memory":
            mems = storage.get_tenant_memories(tenant_id)
            print(f"\nPrivate Memories for '{tenant_id}': {mems or 'Empty'}\n")
            continue
        if user_input.lower() == "/skills":
            skills = storage.get_global_skills()
            print(f"\nShared Global Skills (All Tenants): {skills or 'Empty'}\n")
            continue
        if user_input.lower() == "/budget":
            print(f"\nLast Run Budget Summary: {budget.summary()}\n")
            continue
        if user_input.lower() == "/clear":
            storage.clear_session(tenant_id)
            print(f"  [Cleared session turns for '{tenant_id}'.]\n")
            continue

        try:
            reply = run_active(user_input)
            print(f"\nSwarm: {reply}")
            print(f"  [Budget Summary] {budget.summary()}\n")
        except Exception as exc:
            print(f"\n[Error] {exc}\n")


if __name__ == "__main__":
    main()
