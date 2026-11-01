"""
tests/unit/test_task_driven_selection.py - The swarm shape must follow the task, unless chosen manually.

Covers the deterministic work-shape -> topology mapping, team-size validation, the new single-agent
topology, manual overrides, and the absence of hardcoded domain behaviour in the core.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.checkpoint import CheckpointStore
from core.config import CONFIG
from core.llm import LLMResponse, strip_tool_call_text
from core.meta_planner import (
    SwarmGenerationValidator,
    TaskDrivenMetaPlanner,
    resolve_topology,
)
from core.mission import IntegratedMissionRunner
from core.registry import DynamicRoleSpec
from core.storage import SQLiteStorage
from core.topology import GLOBAL_TOPOLOGY_REGISTRY, SingleAgentTopology


def _role(name: str, function: str = "specialist", caps: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "name": name,
        "purpose": f"{name} purpose",
        "instructions": f"Act as {name}.",
        "capabilities": caps if caps is not None else [],
        "function": function,
    }


def _plan(shape: str, roles: List[Dict[str, Any]], complexity: str = "moderate") -> Dict[str, Any]:
    return {
        "title": "Plan",
        "task_analysis": {"complexity": complexity, "work_shape": shape, "rationale": "because"},
        "roles": roles,
        "budgets": {"max_swarm_steps": 16, "max_total_tokens": 20_000},
    }


class PlannerLLM:
    """Returns queued planner payloads, then plain text for any worker call."""

    model = "stub-model"

    def __init__(self, *plans: Dict[str, Any]) -> None:
        self.plans = list(plans)
        self.prompts: List[str] = []
        self.system_prompts: List[str] = []

    def generate(self, messages: List[Dict[str, Any]], tools: Any = None, **kwargs: Any) -> LLMResponse:
        if tools and tools[0]["function"]["name"] == "emit_structured_output":
            self.system_prompts.append(messages[0]["content"])
            self.prompts.append(messages[-1]["content"])
            return LLMResponse(content=json.dumps(self.plans.pop(0)))
        return LLMResponse(content="APPROVED worker output")


def _runner(llm: PlannerLLM, tmp: Path) -> IntegratedMissionRunner:
    return IntegratedMissionRunner(
        llm=llm,
        checkpoint_store=CheckpointStore(db_path=str(tmp / "cp.db"), hmac_secret=b"x" * 32),
        storage=SQLiteStorage(db_path=str(tmp / "s.db")),
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "shape,roles,expected",
    [
        ("atomic", [_role("solo")], "single"),
        ("independent_parts", [_role("a"), _role("b")], "orchestrator"),
        ("sequential_stages", [_role("a"), _role("b"), _role("c")], "pipeline"),
        ("draft_and_review", [_role("writer"), _role("editor", "verifier")], "review_loop"),
        ("interdependent", [_role("a", "discovery"), _role("b"), _role("c", "verifier")], "peer"),
    ],
)
def test_work_shape_selects_topology(shape: str, roles: List[Dict[str, Any]], expected: str) -> None:
    planner = TaskDrivenMetaPlanner(llm=PlannerLLM(_plan(shape, roles)))  # type: ignore[arg-type]
    spec = planner.plan_swarm("any task", tenant_id="acme", swarm_id="s1")
    assert spec.topology == expected
    assert spec.topology_source == "task_analysis"
    assert spec.task_analysis["work_shape"] == shape


@pytest.mark.unit
@pytest.mark.parametrize(
    "shape,count",
    [("atomic", 2), ("draft_and_review", 3), ("sequential_stages", 1), ("interdependent", 2)],
)
def test_team_size_must_fit_work_shape(shape: str, count: int) -> None:
    plan = _plan(shape, [_role(f"r{i}") for i in range(count)])
    ok, err = SwarmGenerationValidator().validate_spec(plan)
    assert ok is False
    assert shape in err or resolve_topology(plan)[0] in err


@pytest.mark.unit
def test_invalid_team_is_repaired_with_feedback() -> None:
    bad = _plan("atomic", [_role("a"), _role("b")])
    good = _plan("atomic", [_role("a")])
    llm = PlannerLLM(bad, good)
    spec = TaskDrivenMetaPlanner(llm=llm).plan_swarm("2+2?", tenant_id="acme", swarm_id="s1")  # type: ignore[arg-type]
    assert spec.topology == "single"
    assert len(llm.plans) == 0


@pytest.mark.unit
def test_plan_without_any_shape_is_rejected() -> None:
    plan = {"title": "x", "roles": [_role("a")], "budgets": {"max_swarm_steps": 4, "max_total_tokens": 1000}}
    ok, _ = SwarmGenerationValidator().validate_spec(plan)
    assert ok is False


@pytest.mark.unit
def test_planner_output_cannot_smuggle_tools_or_model_policy() -> None:
    role = {**_role("a"), "tool_list": ["self_heal_codebase", "run_python"], "model_policy": {"model": "x"}}
    spec = TaskDrivenMetaPlanner(llm=PlannerLLM(_plan("atomic", [role]))).plan_swarm(  # type: ignore[arg-type]
        "x", tenant_id="acme", swarm_id="s1"
    )
    assert spec.roles[0].tool_list == []
    assert spec.roles[0].model_policy == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"limits": {"max_steps": 1}}, CONFIG.min_role_steps),
        ({"max_steps": 1}, CONFIG.min_role_steps),
        ({"limits": {"max_steps": 99}}, 99),
    ],
)
def test_planner_cannot_starve_a_role_of_steps(fields: Dict[str, Any], expected: int) -> None:
    """One step lets a role call a tool but never read the result or answer."""
    role = {**_role("a"), **fields}
    spec = TaskDrivenMetaPlanner(llm=PlannerLLM(_plan("atomic", [role]))).plan_swarm(  # type: ignore[arg-type]
        "x", tenant_id="acme", swarm_id="s1"
    )
    assert spec.roles[0].max_steps == expected


@pytest.mark.unit
def test_planner_prompt_is_domain_neutral_and_prefers_small_teams() -> None:
    llm = PlannerLLM(_plan("atomic", [_role("a")]))
    TaskDrivenMetaPlanner(llm=llm).plan_swarm("Write a haiku", tenant_id="acme", swarm_id="s1")  # type: ignore[arg-type]
    prompt = llm.system_prompts[0].lower()
    for audit_word in ("recon", "dom structure", "http headers", "endpoint", "hypothesis"):
        assert audit_word not in prompt
    assert "smallest team" in prompt
    assert "@self_heal" not in prompt


@pytest.mark.integration
def test_atomic_task_runs_one_agent(tmp_path: Path) -> None:
    llm = PlannerLLM(_plan("atomic", [_role("solver", caps=["@code"])], complexity="simple"))
    result = _runner(llm, tmp_path).execute("What is 2 + 2?", tenant_id="acme")
    assert result.spec["topology"] == "single"
    assert result.spec["topology_source"] == "task_analysis"
    assert result.spec["roles"] == ["solver"]
    assert result.status == "COMPLETED"


@pytest.mark.integration
def test_three_role_plan_is_not_forced_into_peer(tmp_path: Path) -> None:
    plan = _plan("independent_parts", [_role("a"), _role("b"), _role("c")])
    result = _runner(PlannerLLM(plan), tmp_path).execute("Three unrelated questions", tenant_id="acme")
    assert result.spec["topology"] == "orchestrator"


@pytest.mark.integration
@pytest.mark.parametrize("manual", ["pipeline", "peer", "review_loop", "orchestrator"])
def test_manual_choice_beats_task_analysis(tmp_path: Path, manual: str) -> None:
    llm = PlannerLLM(_plan("atomic", [_role("a")]))
    planner_spec = _runner(llm, tmp_path).plan("Anything", tenant_id="acme", topology_override=manual)
    assert planner_spec.topology == manual
    assert planner_spec.topology_source == "manual"
    assert manual in llm.prompts[0]


@pytest.mark.integration
@pytest.mark.parametrize("auto", ["auto", "", None])
def test_auto_means_task_decides(tmp_path: Path, auto: Optional[str]) -> None:
    spec = _runner(PlannerLLM(_plan("atomic", [_role("a")])), tmp_path).plan(
        "x", tenant_id="acme", topology_override=auto
    )
    assert spec.topology == "single"


@pytest.mark.integration
def test_unknown_manual_topology_fails_fast(tmp_path: Path) -> None:
    runner = _runner(PlannerLLM(_plan("atomic", [_role("a")])), tmp_path)
    with pytest.raises(ValueError, match="Unknown topology"):
        runner.execute("x", tenant_id="acme", topology_override="hexagon")


@pytest.mark.integration
def test_budget_scales_with_complexity_and_stays_under_ceiling(tmp_path: Path) -> None:
    simple = _plan("atomic", [_role("a")], complexity="simple")
    complex_plan = _plan("interdependent", [_role("a"), _role("b"), _role("c")], complexity="complex")
    complex_plan["budgets"] = {"max_swarm_steps": 128, "max_total_tokens": 500_000}
    runner = _runner(PlannerLLM(simple, complex_plan), tmp_path)
    small, big = runner.plan("x", tenant_id="acme"), runner.plan("y", tenant_id="acme")
    assert small.budgets.max_total_tokens < big.budgets.max_total_tokens
    assert big.budgets.max_swarm_steps <= 128 and big.budgets.max_total_tokens <= 500_000


@pytest.mark.integration
def test_planner_cannot_set_the_wall_clock_limit(tmp_path: Path) -> None:
    """A model guessing 10s would kill a healthy mission on a slow local model."""
    plan = _plan("atomic", [_role("a")])
    plan["budgets"]["max_wall_seconds"] = 10
    spec = _runner(PlannerLLM(plan), tmp_path).plan("x", tenant_id="acme")
    assert spec.budgets.max_wall_seconds == CONFIG.max_wall_seconds


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Answer", "Answer"),
        ('<tool_call>{"name": "x"}</tool_call>', ""),
        ('Done.\n<tool_call>{"name": "x"}</tool_call>', "Done."),
        ('Done.\n<tool_call>{"name": "x"', "Done."),
    ],
)
def test_tool_call_text_never_reaches_the_answer(raw: str, expected: str) -> None:
    assert strip_tool_call_text(raw) == expected


@pytest.mark.integration
def test_previewed_plan_runs_without_replanning(tmp_path: Path) -> None:
    llm = PlannerLLM(_plan("atomic", [_role("solver")]))
    runner = _runner(llm, tmp_path)
    preview = runner.plan("x", tenant_id="acme").to_dict()
    preview["roles"][0]["instructions"] = "Edited by a human."
    result = runner.execute("x", tenant_id="acme", preset_spec=preview)
    assert result.spec["roles"] == ["solver"]
    assert llm.plans == [] and len(llm.prompts) == 1


@pytest.mark.integration
def test_edited_plan_cannot_exceed_privilege_ceiling(tmp_path: Path) -> None:
    runner = _runner(PlannerLLM(_plan("atomic", [_role("solver")])), tmp_path)
    preview = runner.plan("x", tenant_id="acme").to_dict()
    preview["roles"][0]["capabilities"] = ["@self_heal"]
    with pytest.raises(ValueError, match="Privilege Ceiling"):
        runner.execute("x", tenant_id="acme", preset_spec=preview)


@pytest.mark.unit
def test_single_topology_is_registered_and_runs_only_its_role(tmp_path: Path) -> None:
    assert GLOBAL_TOPOLOGY_REGISTRY.get("single") is SingleAgentTopology
    bus = EventBus()
    topo = SingleAgentTopology(
        tenant_id="acme",
        roles=[DynamicRoleSpec.from_dict({"name": "solo", "purpose": "p", "instructions": "i"})],
        llm=PlannerLLM(),  # type: ignore[arg-type]
        budget=BudgetTracker(config=BudgetConfig(), bus=bus, tenant_id="acme"),
        bus=bus,
        storage=SQLiteStorage(db_path=str(tmp_path / "s.db")),
    )
    assert topo.execute("task") == "APPROVED worker output"


@pytest.mark.unit
def test_core_has_no_role_name_sniffing() -> None:
    source = Path("core/peer_swarm.py").read_text(encoding="utf-8")
    for needle in ('"critic"', '"recon"', '"verifier"', '"reviewer"'):
        assert needle not in source.split("class PeerSwarm")[1].replace('"verifier",', "") or needle == '"verifier"'
    assert "_is_critic_role(name: str)" not in source


@pytest.mark.unit
def test_seed_roles_declare_function_as_data() -> None:
    roles = {r["name"]: r for r in json.loads(Path("config/default_roles.json").read_text(encoding="utf-8"))}
    assert roles["critic"]["function"] == "verifier"
    assert roles["researcher"]["function"] == "discovery"


@pytest.mark.unit
def test_default_prompts_do_not_assume_an_audit() -> None:
    settings = json.loads(Path("config/settings.json").read_text(encoding="utf-8"))
    joined = " ".join(settings["prompts"].values()).lower()
    for word in ("hypothesis", "probing", "endpoint", "vector"):
        assert word not in joined
