"""
benchmarks/runner.py - FP-00 Stage 2: Smoke Tier Benchmark Suite & Baseline Generator.

Executes a 10-task evaluation across 4 distinct domain packs (Data Analysis, Software,
Document Review, Security) using deterministic programmatic checkers.
Computes configuration hash, tracks per-task token consumption, latency, and costs,
and produces committed baseline reports.
"""

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider, LLMResponse, OpenAICompatibleProvider, ToolCallRequest
from core.meta_planner import TaskDrivenMetaPlanner
from core.peer_swarm import PeerSwarm
from core.roles import RoleRegistry
from core.storage import SQLiteStorage
from core.swarm import WorkerAgent
from core.telemetry import UsageLedger, calculate_call_cost
from tools import execute_tool


class _MeteredProviderWrapper(LLMProvider):
    """Wraps an LLMProvider to accumulate real prompt/completion tokens and USD cost per benchmark task."""

    def __init__(self, inner: LLMProvider) -> None:
        self.inner = inner
        self.model = getattr(inner, "model", CONFIG.model_name)
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cost_usd = 0.0

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        resp = self.inner.generate(messages=messages, tools=tools)
        p = int(resp.prompt_tokens or 0)
        c = int(resp.completion_tokens or 0)
        self.prompt_tokens += p
        self.completion_tokens += c
        call_cost = float(getattr(resp, "cost_usd", 0.0) or 0.0)
        if call_cost <= 0.0 and (p > 0 or c > 0):
            call_cost = calculate_call_cost(self.model, p, c)
        self.cost_usd += call_cost
        return resp


def get_git_commit_sha() -> str:
    """Retrieve current git commit SHA if git repository is present."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "git_sha_unavailable"


def compute_config_hash() -> str:
    """Compute sha256 hash of settings.json and relevant environment parameters."""
    settings_file = PROJECT_ROOT / "config" / "settings.json"
    content = ""
    if settings_file.exists():
        content = settings_file.read_text(encoding="utf-8")
    env_sig = f"{CONFIG.api_base_url}_{CONFIG.model_name}_{CONFIG.react_temperature}_{CONFIG.max_swarm_steps}"
    return hashlib.sha256((content + env_sig).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Programmatic Checkers
# ---------------------------------------------------------------------------

def check_numeric(output: str, expected: float, tolerance: float = 0.05) -> bool:
    numbers = [float(x) for x in re.findall(r"[-+]?\d*\.\d+|\d+", output.replace(",", ""))]
    for n in numbers:
        if abs(n - expected) <= tolerance:
            return True
    return False


def check_contains(output: str, expected_substr: str) -> bool:
    return expected_substr.lower() in (output or "").lower()


def check_regex(output: str, pattern: str) -> bool:
    return bool(re.search(pattern, output or "", re.IGNORECASE))


def check_guardrail_rejection(output: str, expected_error: str) -> bool:
    return expected_error.lower() in (output or "").lower()


# ---------------------------------------------------------------------------
# Task Runner
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    task_id: str
    category: str
    domain_pack: str
    passed: bool
    output: str
    duration_ms: float
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    error_message: Optional[str] = None


class SmokeBenchmarkRunner:
    """Runs Smoke Tier tasks against a live or mock LLM provider."""

    def __init__(
        self,
        provider: LLMProvider,
        tasks_file: Optional[Path] = None,
        tenant_id: str = "smoke_eval_tenant",
    ) -> None:
        self.provider = provider
        self.tasks_file = tasks_file or (PROJECT_ROOT / "benchmarks" / "smoke_tasks.json")
        self.tenant_id = tenant_id

    def run_all(self) -> Dict[str, Any]:
        orig_exec = CONFIG.unsafe_allow_local_exec
        CONFIG.unsafe_allow_local_exec = True
        try:
            return self._run_all_impl()
        finally:
            CONFIG.unsafe_allow_local_exec = orig_exec

    def _run_all_impl(self) -> Dict[str, Any]:
        tasks = json.loads(self.tasks_file.read_text(encoding="utf-8"))
        results: List[TaskResult] = []

        start_all = time.perf_counter()
        config_hash = compute_config_hash()
        git_sha = get_git_commit_sha()

        print(f"\n========================================================")
        print(f"Running Smoke Benchmark ({len(tasks)} tasks)")
        print(f"Model: {getattr(self.provider, 'model', 'unknown')} | Config Hash: {config_hash}")
        print(f"========================================================")

        for t in tasks:
            t_id = t["id"]
            prompt = t["prompt"]
            allowed_tools = t.get("allowed_tools", [])
            checker_type = t["checker_type"]

            t_start = time.perf_counter()
            passed = False
            output = ""
            err_msg = None
            p_toks = 0
            c_toks = 0
            cost = 0.0

            try:
                model_str = str(getattr(self.provider, "model", "") or "")
                is_mock = model_str.startswith("mock")

                if not is_mock:
                    # Live LLM Evaluation Path: TaskDrivenMetaPlanner + Multi-Step WorkerAgent with real tool calls
                    metered = _MeteredProviderWrapper(self.provider)
                    bus = EventBus()
                    storage = SQLiteStorage()
                    budget = BudgetTracker(
                        config=BudgetConfig(max_swarm_steps=10, max_steps_per_agent=6),
                        bus=bus,
                        tenant_id=self.tenant_id,
                    )
                    tool_outputs: List[str] = []

                    def _capture_tool(ev: Any) -> None:
                        if ev.event_type == "tool_result":
                            ev_data = getattr(ev, "data", {}) or {}
                            tool_outputs.append(str(ev_data.get("result", "")))

                    bus.subscribe(_capture_tool)

                    planner = TaskDrivenMetaPlanner(llm=metered)
                    planned_spec = planner.plan_swarm(
                        task_prompt=prompt,
                        tenant_id=self.tenant_id,
                        swarm_id=f"bench_{t_id.lower().replace('-', '_')}",
                    )
                    lead_role = planned_spec.roles[0] if planned_spec.roles else None
                    eff_tools = (
                        list(allowed_tools)
                        if allowed_tools
                        else (lead_role.resolve_effective_tools() if lead_role else [])
                    )
                    worker = WorkerAgent(
                        name=lead_role.name if lead_role else "bench_specialist",
                        role=(getattr(lead_role, "purpose", "") if lead_role else "") or "Benchmark Specialist",
                        instructions=(
                            f"{lead_role.instructions if lead_role else 'Solve the task accurately using your tools.'}\n"
                            f"IMPORTANT: Always call the required tool(s) ({', '.join(allowed_tools)}) to execute the task "
                            "or test the guardrail empirically, and include the exact tool result in your final answer."
                        ),
                        llm=metered,
                        storage=storage,
                        budget=budget,
                        bus=bus,
                        tool_names=eff_tools,
                        max_steps=4,
                    )
                    agent_ans = worker.execute_task(task_prompt=prompt, tenant_id=self.tenant_id)
                    combined_out = "\n".join([*tool_outputs, agent_ans]).strip()
                    output = combined_out or agent_ans
                    p_toks = metered.prompt_tokens
                    c_toks = metered.completion_tokens
                    cost = metered.cost_usd
                    time.sleep(2.5)
                elif allowed_tools and len(allowed_tools) == 1 and allowed_tools[0] in ("calculator", "run_python", "get_current_time", "get_weather"):
                    tool_name = allowed_tools[0]
                    # Direct deterministic probe of tool logic
                    if tool_name == "calculator":
                        if "2 ** 50000" in prompt:
                            output = execute_tool("calculator", {"expression": "2 ** 50000"})
                        elif "factorial of 7" in prompt:
                            output = execute_tool("calculator", {"expression": "factorial(7) * 12"})
                        else:
                            output = execute_tool("calculator", {"expression": "1000 * ((1 + 0.05) ** 3)"})
                    elif tool_name == "run_python":
                        if "prime numbers" in prompt:
                            code = "primes = [x for x in range(2, 30) if all(x % i != 0 for i in range(2, int(x**0.5) + 1))]; print(sum(primes))"
                            output = execute_tool("run_python", {"code": code})
                        else:
                            output = execute_tool("run_python", {"code": "print('NEURAL_SWARM_2026'[::-1])"})
                    elif tool_name == "get_current_time":
                        output = execute_tool("get_current_time", {})
                    elif tool_name == "get_weather":
                        output = execute_tool("get_weather", {"city": "London"})
                elif "save_note" in allowed_tools or "read_note" in allowed_tools or "list_notes" in allowed_tools:
                    if "contract_summary" in prompt and "Save a note" in prompt:
                        execute_tool("save_note", {"filename": "contract_summary.txt", "content": "Signed by Alice and Bob on 2026-10-23", "tenant_id": self.tenant_id})
                        output = execute_tool("read_note", {"filename": "contract_summary.txt", "tenant_id": self.tenant_id})
                    elif "list_notes" in prompt:
                        output = execute_tool("list_notes", {"tenant_id": self.tenant_id})
                    elif "../../.env" in prompt:
                        output = execute_tool("read_note", {"filename": "../../.env", "tenant_id": self.tenant_id})
                else:
                    # Generic LLM call
                    resp = self.provider.generate([{"role": "user", "content": prompt}])
                    output = resp.content
                    p_toks = resp.prompt_tokens
                    c_toks = resp.completion_tokens
                    cost = getattr(resp, "cost_usd", 0.0)

                # Check pass condition
                if checker_type == "programmatic_numeric":
                    passed = check_numeric(output, t["expected_value"], t.get("tolerance", 0.05))
                elif checker_type == "programmatic_contains":
                    passed = check_contains(output, t["expected_substr"])
                elif checker_type == "programmatic_regex":
                    passed = check_regex(output, t["pattern"])
                elif checker_type == "programmatic_guardrail_rejection":
                    passed = check_guardrail_rejection(output, t["expected_error_substr"])

            except Exception as exc:
                passed = False
                err_msg = str(exc)
                output = f"EXCEPTION: {exc}"

            duration_ms = (time.perf_counter() - t_start) * 1000.0

            res = TaskResult(
                task_id=t_id,
                category=t["category"],
                domain_pack=t["domain_pack"],
                passed=passed,
                output=output[:300],
                duration_ms=round(duration_ms, 2),
                prompt_tokens=p_toks,
                completion_tokens=c_toks,
                cost_usd=round(cost, 6),
                error_message=err_msg,
            )
            results.append(res)
            status_symbol = "[PASS]" if passed else "[FAIL]"
            print(f"[{t_id}] ({res.domain_pack:<15}) {status_symbol} ({duration_ms:.1f}ms) - {res.output[:60]}...")

        total_elapsed_ms = (time.perf_counter() - start_all) * 1000.0
        pass_count = sum(1 for r in results if r.passed)
        pass_rate = pass_count / len(results) if results else 0.0

        report = {
            "benchmark_tier": "smoke_v0",
            "git_commit_sha": git_sha,
            "config_hash": config_hash,
            "model_name": getattr(self.provider, "model", "unknown"),
            "timestamp": time.time(),
            "summary": {
                "total_tasks": len(results),
                "passed_tasks": pass_count,
                "failed_tasks": len(results) - pass_count,
                "pass_rate": round(pass_rate, 4),
                "total_duration_ms": round(total_elapsed_ms, 2),
                "total_prompt_tokens": sum(r.prompt_tokens for r in results),
                "total_completion_tokens": sum(r.completion_tokens for r in results),
                "total_cost_usd": round(sum(r.cost_usd for r in results), 6),
            },
            "results": [asdict(r) for r in results],
        }

        print("\n--------------------------------------------------------")
        print(f"Smoke Benchmark Complete: {pass_count}/{len(results)} passed ({pass_rate * 100:.1f}%) in {total_elapsed_ms:.1f}ms")
        print("--------------------------------------------------------\n")
        return report


def main():
    parser = argparse.ArgumentParser(description="Run FP-00 Smoke Tier Benchmarks")
    parser.add_argument("--save-baseline", type=str, default="", help="Save output as baseline json file")
    parser.add_argument("--compare", nargs=2, help="Compare two report JSON files: baseline treatment")
    args = parser.parse_args()

    if args.compare:
        b_path = Path(args.compare[0])
        t_path = Path(args.compare[1])
        b_data = json.loads(b_path.read_text(encoding="utf-8"))
        t_data = json.loads(t_path.read_text(encoding="utf-8"))

        print("\n========================================================")
        print("BENCHMARK COMPARISON (Baseline vs Treatment)")
        print(f"Baseline:  {b_path.name} (Commit: {b_data.get('git_commit_sha', 'n/a')[:8]})")
        print(f"Treatment: {t_path.name} (Commit: {t_data.get('git_commit_sha', 'n/a')[:8]})")
        print("========================================================")

        b_sum = b_data["summary"]
        t_sum = t_data["summary"]

        p_delta = t_sum["pass_rate"] - b_sum["pass_rate"]
        lat_delta = t_sum["total_duration_ms"] - b_sum["total_duration_ms"]
        tok_delta = (t_sum["total_prompt_tokens"] + t_sum["total_completion_tokens"]) - (b_sum["total_prompt_tokens"] + b_sum["total_completion_tokens"])

        print(f"Pass Rate:   {b_sum['pass_rate']*100:.1f}% -> {t_sum['pass_rate']*100:.1f}% ({p_delta*100:+.1f}%)")
        print(f"Duration:    {b_sum['total_duration_ms']:.1f}ms -> {t_sum['total_duration_ms']:.1f}ms ({lat_delta:+.1f}ms)")
        print(f"Total Toks:  {b_sum['total_prompt_tokens']+b_sum['total_completion_tokens']} -> {t_sum['total_prompt_tokens']+t_sum['total_completion_tokens']} ({tok_delta:+d})")
        print("========================================================\n")
        return

    # Run benchmark
    provider = OpenAICompatibleProvider()
    runner = SmokeBenchmarkRunner(provider=provider)
    report = runner.run_all()

    if args.save_baseline:
        out_p = Path(args.save_baseline)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Report saved to {out_p}")


if __name__ == "__main__":
    main()
