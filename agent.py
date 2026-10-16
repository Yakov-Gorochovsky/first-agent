"""
agent.py - Modular Multi-Tenant AI Swarm CLI (Step 2: Orchestrator -> Workers + Budgets).

Uses loosely coupled interfaces from `core/`:
- `core/llm.py`     (LLMProvider: Ollama, Hugging Face, Groq, Gemini, OpenAI)
- `core/storage.py` (StorageBackend: SQLite multi-tenant isolation + PrivacyAirlock global skills)
- `core/budget.py`  (BudgetTracker: step, token, and wall-time caps)
- `core/bus.py`     (EventBus: pub/sub telemetry for CLI and SSE UI)
- `core/swarm.py`   (OrchestratorSwarm & WorkerAgent)
"""

import argparse
import os
from dotenv import load_dotenv

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus, SwarmEvent
from core.llm import OpenAICompatibleProvider
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


def build_swarm() -> tuple[OrchestratorSwarm, SQLiteStorage, BudgetTracker, EventBus]:
    bus = EventBus()
    bus.subscribe(console_event_listener)

    storage = SQLiteStorage(db_path=DB_PATH, max_trace_rows=500)
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
    swarm = OrchestratorSwarm(llm=llm, storage=storage, budget=budget, bus=bus)
    return swarm, storage, budget, bus


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
    parser.add_argument("--new-session", action="store_true", help="Clear tenant session history.")
    args = parser.parse_args()

    swarm, storage, budget, _ = build_swarm()
    tenant_id = args.tenant

    if args.new_session:
        storage.clear_session(tenant_id)

    print("=" * 74)
    print(f" Modular AI Swarm | Model: {MODEL_NAME} | Tenant: '{tenant_id}'")
    print(" Architecture: Orchestrator -> Workers (researcher, coder) + SQLite + Budgets")
    print("=" * 74)

    if args.prompt:
        print(f"\nYou ({tenant_id}): {args.prompt}")
        reply = swarm.run(args.prompt, tenant_id=tenant_id)
        print(f"\nSwarm: {reply}")
        print(f"  [Budget Summary] {budget.summary()}\n")
        return

    print(
        "Commands: '/tenant <id>' switch client | '/memory' private facts | "
        "'/skills' shared global skills | '/budget' | '/clear' | 'exit'\n"
    )
    while True:
        try:
            user_input = input(f"You ({tenant_id}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "q"}:
            print("Goodbye!")
            break
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
            reply = swarm.run(user_input, tenant_id=tenant_id)
            print(f"\nSwarm: {reply}")
            print(f"  [Budget Summary] {budget.summary()}\n")
        except Exception as exc:
            print(f"\n[Error] {exc}\n")


if __name__ == "__main__":
    main()
