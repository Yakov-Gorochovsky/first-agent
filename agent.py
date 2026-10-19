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
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus, SwarmEvent
from core.config import CONFIG
from core.llm import OpenAICompatibleProvider
from core.roles import CAPABILITY_BUNDLES, RoleRegistry
from core.storage import SQLiteStorage
from core.swarm_manager import SwarmManager


def console_event_listener(event: SwarmEvent) -> None:
    """Subscribe to EventBus to render live swarm telemetry in the terminal."""
    if not CONFIG.verbose:
        return
    etype = event.event_type
    agent = event.agent_name
    tenant = event.tenant_id
    data = event.data

    if etype == "delegation":
        print(f"  [{tenant}:Orchestrator -> {data.get('to_worker')}] Task: {data.get('task')}")
    elif etype == "peer_message":
        print(
            f"  [{tenant} Inbox: {data.get('from_agent')} -> {data.get('to_agent')}] "
            f"{str(data.get('message', ''))[:200]}"
        )
    elif etype == "cross_swarm_message":
        print(
            f"  [Cross-Swarm: {data.get('from_swarm')} -> {data.get('to_swarm')}] "
            f"{str(data.get('message', ''))[:220]}"
        )
    elif etype == "cross_swarm_reply":
        print(
            f"  [Cross-Swarm Reply: {data.get('from_swarm')} -> {data.get('to_swarm')}] "
            f"{str(data.get('reply', ''))[:220]}"
        )
    elif etype == "role_created":
        role = data.get("role", {})
        print(
            f"  [RoleRegistry | New Role Spawned] '{role.get('name')}' ({role.get('title')}) "
            f"Tools: {role.get('resolved_tools')}"
        )
    elif etype == "tool_call":
        print(f"  [{tenant}:{agent} | Tool Call] {data.get('tool')}({data.get('arguments')})")
    elif etype == "tool_result":
        res = str(data.get("result", ""))
        preview = res if len(res) <= 220 else res[:220] + " ... [truncated]"
        print(f"  [{tenant}:{agent} | Observation] {preview}")
    elif etype == "skill_learned":
        print(
            f"  [PrivacyAirlock -> Global Skill] [{data.get('category')}] "
            f"{data.get('problem_pattern')} => {data.get('solution_rule')}"
        )


def build_swarms():
    bus = EventBus()
    bus.subscribe(console_event_listener)

    storage = SQLiteStorage()
    role_registry = RoleRegistry()
    budget = BudgetTracker(config=BudgetConfig(), bus=bus)
    llm = OpenAICompatibleProvider()
    swarm_manager = SwarmManager(
        storage=storage, bus=bus, role_registry=role_registry, default_llm=llm
    )
    return swarm_manager, role_registry, storage, budget, bus


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Modular AI Agent Swarm.")
    parser.add_argument("-p", "--prompt", type=str, help="Run a single prompt and exit.")
    parser.add_argument(
        "-t",
        "--tenant",
        type=str,
        default="attacker",
        help="Active Swarm ID / Tenant ID (default: attacker).",
    )
    parser.add_argument(
        "-m",
        "--mode",
        type=str,
        choices=["peer", "orchestrator"],
        default="peer",
        help="Swarm topology override: 'peer' or 'orchestrator'.",
    )
    parser.add_argument("--new-session", action="store_true", help="Clear tenant session history.")
    args = parser.parse_args()

    swarm_manager, role_registry, storage, budget, _ = build_swarms()
    tenant_id = args.tenant
    mode = args.mode

    if args.new_session:
        storage.clear_session(tenant_id)

    swarm_names = ", ".join(s.swarm_id for s in swarm_manager.list_swarms())
    role_names = ", ".join(r.name for r in role_registry.list_roles())
    print("=" * 78)
    print(f" Modular AI Swarm | Model: {CONFIG.model_name} | Active Swarm: '{tenant_id}' | Mode: {mode.upper()}")
    print(f" Deployed Swarms: [{swarm_names}] (Managed via core/swarm_manager.py)")
    print(f" Active Roles:    [{role_names}] (Managed via core/roles.py)")
    print(f" Web UI:          Run `python server.py` and open http://{CONFIG.host}:{CONFIG.port}")
    print("=" * 78)

    def run_active(prompt_text: str) -> str:
        return swarm_manager.run_swarm(
            swarm_id=tenant_id,
            prompt=prompt_text,
            topology_override=mode,
            budget=budget,
        )

    if args.prompt:
        print(f"\nYou ({tenant_id}): {args.prompt}")
        reply = run_active(args.prompt)
        print(f"\nSwarm ({tenant_id}): {reply}")
        return

    print(
        "Commands: '/swarms' | '/roles' | '/add-role <name> | <title> | <@bundles> | <instructions>' | "
        "'/del-role <name>' | '/mode peer|orchestrator' | '/tenant <swarm_id>' | '/memory' | '/skills' | 'exit'\n"
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
        if user_input.lower() == "/swarms":
            print("\nDeployed Swarms:")
            for s in swarm_manager.list_swarms():
                print(
                    f"  - {s.swarm_id} ({s.title}) | Topology: {s.topology} | "
                    f"Cross-Swarm Bridge: {s.allow_cross_swarm} | Roles: {s.roles}"
                )
            print()
            continue
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
            print(f"  [Switched active swarm/tenant to '{tenant_id}'. Private memory is isolated.]\n")
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
            curr_budget = swarm_manager.get_last_budget(tenant_id) or budget
            print(f"\nLast Run Budget Summary: {curr_budget.summary()}\n")
            continue
        if user_input.lower() == "/clear":
            storage.clear_session(tenant_id)
            print(f"  [Cleared session turns for '{tenant_id}'.]\n")
            continue

        try:
            reply = run_active(user_input)
            print(f"\nSwarm: {reply}")
            curr_budget = swarm_manager.get_last_budget(tenant_id) or budget
            print(f"  [Budget Summary] {curr_budget.summary()}\n")
        except Exception as exc:
            print(f"\n[Error] {exc}\n")


if __name__ == "__main__":
    main()
