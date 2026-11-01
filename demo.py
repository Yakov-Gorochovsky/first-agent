"""
demo.py - See task-driven swarm selection work, offline and with no API key.

The model is scripted, so the plans below are canned. Everything after the model's reply is the real
code path: validation, work shape to topology mapping, the factory, execution, and signed checkpoints.
This shows the plumbing. It says nothing about model quality; see benchmarks/README.md for that.
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.bus import EventBus
from core.checkpoint import CheckpointStore
from core.llm import LLMProvider, LLMResponse
from core.mission import IntegratedMissionRunner, MissionExecutionResult
from core.storage import SQLiteStorage

DEMO_HMAC_SECRET = b"demo-only-not-a-secret-value-0000"


def _role(name: str, purpose: str, function: str = "specialist") -> Dict[str, Any]:
    return {"name": name, "purpose": purpose, "instructions": f"Act as the {purpose}.", "capabilities": [], "function": function}


def _plan(domain: str, shape: str, rationale: str, roles: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "title": f"{domain} team",
        "task_analysis": {"domain": domain, "complexity": "simple", "work_shape": shape, "rationale": rationale},
        "roles": roles,
        "budgets": {"max_swarm_steps": 12, "max_total_tokens": 30_000},
    }


SCENARIOS: List[Tuple[str, Optional[str], Dict[str, Any]]] = [
    (
        "What is 15% of 240?",
        None,
        _plan("arithmetic", "atomic", "One calculation; nothing to divide.", [_role("calculator_clerk", "arithmetic clerk")]),
    ),
    (
        "Summarise this lease, translate the summary to Spanish, then format it as a letter.",
        None,
        _plan(
            "legal documents",
            "sequential_stages",
            "Each stage transforms the previous stage's output.",
            [_role("lease_analyst", "lease analyst"), _role("translator", "translator"), _role("letter_formatter", "letter formatter")],
        ),
    ),
    (
        "Write a grant proposal abstract and have it held to reviewer standards.",
        None,
        _plan(
            "grant writing",
            "draft_and_review",
            "One artifact is drafted and improved.",
            [_role("grant_writer", "grant writer"), _role("panel_reviewer", "review panelist", "verifier")],
        ),
    ),
    (
        "Compare nutrition, cost, and storage life for three lunch options.",
        None,
        _plan(
            "nutrition",
            "independent_parts",
            "The three comparisons do not depend on each other.",
            [_role("dietitian", "dietitian"), _role("budget_analyst", "budget analyst"), _role("food_scientist", "food scientist")],
        ),
    ),
    (
        "What is 15% of 240?",
        "pipeline",
        _plan(
            "arithmetic",
            "atomic",
            "The operator chose the topology by hand.",
            [_role("estimator", "estimator"), _role("checker", "checker", "verifier")],
        ),
    ),
]


class ScriptedLLM(LLMProvider):
    """Returns a canned plan to the planner and canned text to every worker."""

    model = "scripted-demo"

    def __init__(self, plan: Dict[str, Any]) -> None:
        self.plan = plan

    def generate(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, **kwargs: Any) -> LLMResponse:
        if tools and tools[0]["function"]["name"] == "emit_structured_output":
            return LLMResponse(content=json.dumps(self.plan))
        return LLMResponse(content="APPROVED: scripted result.")


def run_scenario(prompt: str, manual: Optional[str], plan: Dict[str, Any], workdir: Path) -> MissionExecutionResult:
    runner = IntegratedMissionRunner(
        llm=ScriptedLLM(plan),
        bus=EventBus(),
        checkpoint_store=CheckpointStore(db_path=str(workdir / "checkpoints.db"), hmac_secret=DEMO_HMAC_SECRET),
        storage=SQLiteStorage(db_path=str(workdir / "swarm.db")),
    )
    return runner.execute(prompt, tenant_id="demo", topology_override=manual)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("Scripted model: this shows the selection path, not model quality.\n")
    with tempfile.TemporaryDirectory(prefix="swarm-demo-") as workdir:
        for prompt, manual, plan in SCENARIOS:
            result = run_scenario(prompt, manual, plan, Path(workdir))
            spec = result.spec
            print(f"Task:     {prompt}")
            print(f"Topology: {spec['topology']} (chosen by: {spec['topology_source']})")
            print(f"Why:      {spec['task_analysis'].get('rationale')}")
            print(f"Team:     {', '.join(spec['roles'])}")
            print(f"Result:   {result.status}, {result.checkpoints_count} signed checkpoints\n")


if __name__ == "__main__":
    main()
