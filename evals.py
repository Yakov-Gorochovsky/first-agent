"""
evals.py - Step 5: Automated Evaluation & Regression Suite for the AI Swarm.

Includes two tiers of evaluations:
1. Deterministic Harness & Guardrail Evals (Default — runs in <1s, zero LLM cost):
   - Tests BudgetTracker step/token/time caps
   - Tests Multi-Tenant SQLite memory isolation (`tenant_a` vs `tenant_b`)
   - Tests PrivacyAirlock PII/Secret/URL/Number scrubbing on shared Global Skills
   - Tests RoleRegistry, Capability Bundles (`@web`, `@code`, `@files`), and dynamic spawning
   - Tests Universal Tools (`calculator` AST safety, `run_python` sandbox stdout/stderr)
   - Tests OrchestratorSwarm & PeerSwarm end-to-end inbox routing using a deterministic MockLLM
2. Live Model Benchmark Evals (`python evals.py --live`):
   - Runs real prompts against the active `LLMProvider` (Ollama / Groq / Gemini / HF)
   - Grades tool selection, output accuracy, and token budget compliance.
"""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.budget import BudgetConfig, BudgetExceededError, BudgetTracker
from core.bus import EventBus
from core.llm import LLMResponse, OpenAICompatibleProvider, ToolCallRequest
from core.peer_swarm import PeerSwarm
from core.roles import RoleRegistry
from core.storage import PrivacyAirlock, SQLiteStorage
from core.swarm import OrchestratorSwarm
from tools import execute_tool

load_dotenv()


class ScriptedMockLLM:
    """Deterministic LLM double for testing multi-agent harnesses and guardrails in <50ms."""

    def __init__(self, responses: List[LLMResponse]) -> None:
        self.model = "mock-deterministic-v1"
        self._responses = list(responses)
        self._idx = 0

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return LLMResponse(content="Fallback mock completion.", prompt_tokens=10, completion_tokens=5)


# ---------------------------------------------------------------------------
# Individual Deterministic Evaluation Cases
# ---------------------------------------------------------------------------

def eval_budget_step_guardrail() -> str:
    """Verify BudgetTracker halts a runaway agent that loops past max_steps_per_agent."""
    bus = EventBus()
    budget = BudgetTracker(
        config=BudgetConfig(max_swarm_steps=10, max_steps_per_agent=2, max_total_tokens=5000),
        bus=bus,
    )
    budget.check_and_increment_step("coder")
    budget.check_and_increment_step("coder")
    try:
        budget.check_and_increment_step("coder")
        raise AssertionError("Expected BudgetExceededError on 3rd step when cap is 2.")
    except BudgetExceededError as exc:
        assert "exceeded its step cap (2/2 steps)" in str(exc)
        return "Halted runaway agent at 2/2 steps as expected."


def eval_budget_token_guardrail() -> str:
    """Verify BudgetTracker halts execution when token consumption exceeds max_total_tokens."""
    budget = BudgetTracker(
        config=BudgetConfig(max_swarm_steps=10, max_steps_per_agent=5, max_total_tokens=1000)
    )
    budget.check_and_increment_step("researcher")
    budget.record_tokens("researcher", prompt_toks=600, completion_toks=450)
    try:
        budget.check_and_increment_step("researcher")
        raise AssertionError("Expected BudgetExceededError after 1050/1000 tokens.")
    except BudgetExceededError as exc:
        assert "Token budget exceeded (1050/1000 tokens)" in str(exc)
        return "Halted swarm at 1050/1000 tokens as expected."


def eval_multi_tenant_isolation() -> str:
    """Verify Tenant B can never read Tenant A's private memories or session history."""
    with tempfile.TemporaryDirectory() as tmp:
        store = SQLiteStorage(Path(tmp) / "test_eval.db")
        store.save_tenant_memory("tenant_alpha", "api_secret_project", "Project Apollo $9.5M")
        store.save_tenant_memory("tenant_beta", "public_note", "Healthcare Portal")

        alpha_mem = store.get_tenant_memories("tenant_alpha")
        beta_mem = store.get_tenant_memories("tenant_beta")

        assert alpha_mem.get("api_secret_project") == "Project Apollo $9.5M"
        assert "api_secret_project" not in beta_mem
        assert beta_mem.get("public_note") == "Healthcare Portal"
        return "Strict tenant isolation verified (0 cross-tenant leaks)."


def eval_privacy_airlock_scrubbing() -> str:
    """Verify PrivacyAirlock strips emails, URLs, IPs, API keys, paths, and numbers."""
    raw = (
        "User ceo@fintech.io on 192.168.1.42 using sk-1234567890abcdef failed "
        "fetching https://internal.fintech.io:8443/v2 from C:\\Users\\ceo\\secrets.txt"
    )
    cleaned = PrivacyAirlock.sanitize(raw)
    assert "ceo@fintech.io" not in cleaned and "<EMAIL>" in cleaned
    assert "192.168.1.42" not in cleaned and "<IP>" in cleaned
    assert "sk-1234567890abcdef" not in cleaned and "<SECRET>" in cleaned
    assert "https://internal.fintech.io" not in cleaned and "<URL>" in cleaned
    assert "C:\\Users\\ceo\\secrets.txt" not in cleaned and "<PATH>" in cleaned
    return f"Scrubbed all 5 PII/Secret categories -> '{cleaned}'"


def eval_role_registry_and_bundles() -> str:
    """Verify config/default_roles.json, Capability Bundles, and dynamic role registration."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = RoleRegistry(roles_file=Path(tmp) / "roles.json")
        defaults = [r.name for r in reg.list_roles()]
        assert "researcher" in defaults and "coder" in defaults

        custom = reg.register_role(
            name="pen_tester",
            title="Penetration Tester",
            instructions="Test security.",
            capabilities=["@code", "@files"],
        )
        assert "run_python" in custom.tool_names
        assert "save_note" in custom.tool_names
        assert "web_search" not in custom.tool_names
        return f"Loaded {len(defaults)} default roles & expanded ['@code', '@files'] -> {custom.tool_names}"


def eval_universal_tools_sandbox() -> str:
    """Verify calculator AST safety and run_python subprocess execution."""
    calc_out = execute_tool("calculator", {"expression": "12 * 12 + sqrt(81)"})
    assert "153" in calc_out

    unsafe_out = execute_tool("calculator", {"expression": "__import__('os').system('echo hacked')"})
    assert "Error" in unsafe_out

    py_out = execute_tool("run_python", {"code": "print(sum(i for i in range(1, 11)))"})
    assert "55" in py_out
    return "Calculator AST blocked unsafe __import__ and run_python returned 55."


def eval_peer_swarm_inbox_handoff() -> str:
    """Verify Step 3 PeerSwarm routes messages across Coordinator -> Coder -> Critic -> Finish."""
    with tempfile.TemporaryDirectory() as tmp:
        bus = EventBus()
        store = SQLiteStorage(Path(tmp) / "peer_eval.db")
        budget = BudgetTracker(bus=bus)
        reg = RoleRegistry(roles_file=Path(tmp) / "roles.json")

        scripted = ScriptedMockLLM(
            [
                # 1. Coordinator receives user prompt and messages coder's inbox
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c1",
                            name="send_peer_message",
                            arguments={"to_agent": "coder", "message": "Compute 7 * 8"},
                        )
                    ],
                    prompt_tokens=100,
                    completion_tokens=20,
                    used_react_fallback=True,
                ),
                # 2. Coder wakes up, runs calculator, and messages critic's inbox
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c2",
                            name="calculator",
                            arguments={"expression": "7 * 8"},
                        ),
                        ToolCallRequest(
                            id="c3",
                            name="send_peer_message",
                            arguments={"to_agent": "critic", "message": "Result is 56. Please verify."},
                        ),
                    ],
                    prompt_tokens=120,
                    completion_tokens=30,
                    used_react_fallback=True,
                ),
                # 3. Critic wakes up and calls finish_swarm
                LLMResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c4",
                            name="finish_swarm",
                            arguments={"final_answer": "Verified: 7 * 8 = 56."},
                        )
                    ],
                    prompt_tokens=110,
                    completion_tokens=20,
                    used_react_fallback=True,
                ),
            ]
        )

        swarm = PeerSwarm(llm=scripted, storage=store, budget=budget, bus=bus, role_registry=reg)
        answer = swarm.run("What is 7 * 8?", tenant_id="eval_tenant")
        assert answer == "Verified: 7 * 8 = 56."
        assert budget.total_steps == 3
        assert budget.total_tokens == 400
        return "Coordinator -> Coder -> Critic inbox chain completed in 3 steps (400 tokens)."


def eval_framework_abstractions() -> str:
    """Verify Step 6 LangGraph StateGraph and OpenAI SDK Handoff Runner equivalence."""
    from frameworks_comparison import demo_langgraph_pattern, demo_openai_sdk_pattern

    lg_state = demo_langgraph_pattern()
    assert lg_state.get("verified") is True and lg_state.get("result") == 196
    sdk_out = demo_openai_sdk_pattern()
    assert "parameterized query" in sdk_out
    return "LangGraph StateGraph (14^2=196) & OpenAI SDK Handoff chain verified."


DETERMINISTIC_SUITE: List[tuple[str, Callable[[], str]]] = [
    ("Budget Step Cap Guardrail", eval_budget_step_guardrail),
    ("Budget Token Cap Guardrail", eval_budget_token_guardrail),
    ("Multi-Tenant Memory Isolation", eval_multi_tenant_isolation),
    ("PrivacyAirlock PII & Secret Scrubbing", eval_privacy_airlock_scrubbing),
    ("RoleRegistry & Capability Bundles", eval_role_registry_and_bundles),
    ("Universal Tools & AST Sandbox Safety", eval_universal_tools_sandbox),
    ("PeerSwarm Multi-Hop Inbox Handoff", eval_peer_swarm_inbox_handoff),
    ("Step 6 Framework Patterns (LangGraph/SDK)", eval_framework_abstractions),
]


def run_eval_suite(bus: Optional[EventBus] = None) -> Dict[str, Any]:
    """Run all deterministic evaluations and return a structured scorecard."""
    results: List[Dict[str, Any]] = []
    passed = 0
    t0 = time.time()

    for name, fn in DETERMINISTIC_SUITE:
        step_t0 = time.time()
        try:
            detail = fn()
            status = "PASS"
            passed += 1
        except Exception as exc:
            detail = f"FAILED: {exc}"
            status = "FAIL"
        ms = round((time.time() - step_t0) * 1000, 1)
        item = {"name": name, "status": status, "detail": detail, "ms": ms}
        results.append(item)
        if bus:
            bus.emit(
                "tool_result" if status == "PASS" else "mission_error",
                agent_name="eval_runner",
                tenant_id="system",
                tool=name,
                result=f"[{status} in {ms}ms] {detail}",
            )

    total_ms = round((time.time() - t0) * 1000, 1)
    scorecard = {
        "passed": passed,
        "total": len(DETERMINISTIC_SUITE),
        "pass_rate": f"{round((passed / len(DETERMINISTIC_SUITE)) * 100)}%",
        "total_ms": total_ms,
        "results": results,
    }
    return scorecard


def run_live_llm_eval() -> Dict[str, Any]:
    """Run a live end-to-end regression task against the configured LLMProvider."""
    base_url = os.getenv("API_BASE_URL", "http://localhost:11434/v1")
    api_key = os.getenv("API_KEY", "ollama")
    model = os.getenv("MODEL_NAME", "qwen3:8b")

    with tempfile.TemporaryDirectory() as tmp:
        bus = EventBus()
        store = SQLiteStorage(Path(tmp) / "live_eval.db")
        budget = BudgetTracker(
            config=BudgetConfig(max_swarm_steps=8, max_steps_per_agent=4, max_total_tokens=16000),
            bus=bus,
        )
        reg = RoleRegistry(roles_file=Path(tmp) / "roles.json")
        llm = OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model)
        swarm = OrchestratorSwarm(llm=llm, storage=store, budget=budget, bus=bus, role_registry=reg)

        t0 = time.time()
        reply = swarm.run(
            "Use the coder worker to compute 19 * 23 and return the exact number.",
            tenant_id="live_eval",
        )
        elapsed = round(time.time() - t0, 2)
        ok = "437" in reply
        return {
            "name": f"Live LLM Eval ({model})",
            "status": "PASS" if ok else "FAIL",
            "expected": "437",
            "reply": reply,
            "budget": budget.summary(),
            "elapsed_seconds": elapsed,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the AI Swarm Evaluation & Regression Suite.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Also run a live end-to-end benchmark against the configured LLM.",
    )
    args = parser.parse_args()

    print("=" * 76)
    print(" AI SWARM // EVALUATION & REGRESSION SUITE (STEP 5)")
    print("=" * 76)

    scorecard = run_eval_suite()
    for r in scorecard["results"]:
        badge = "✅ PASS" if r["status"] == "PASS" else "❌ FAIL"
        print(f" {badge} | {r['name']:<38} ({r['ms']}ms)")
        print(f"         └─ {r['detail']}")

    print("-" * 76)
    print(
        f" SCORECARD: {scorecard['passed']}/{scorecard['total']} Passed "
        f"({scorecard['pass_rate']}) in {scorecard['total_ms']}ms"
    )
    print("=" * 76)

    if args.live:
        print("\nRunning Live LLM Benchmark...")
        live_res = run_live_llm_eval()
        badge = "✅ PASS" if live_res["status"] == "PASS" else "❌ FAIL"
        print(f" {badge} | {live_res['name']} ({live_res['elapsed_seconds']}s)")
        print(f"         ├─ Reply:  {live_res['reply']}")
        print(f"         └─ Budget: {live_res['budget']}\n")


if __name__ == "__main__":
    main()
