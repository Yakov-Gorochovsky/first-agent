"""
tests/unit/test_local_model_reliability.py - Slow models must finish; hopeless errors must fail fast.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.factory import SwarmSpec
from core.llm import LLMResponse, ToolCallRequest
from core.mission import IntegratedMissionRunner
from core.resilience import RetryConfig, retry_with_backoff
from core.storage import SQLiteStorage
from core.swarm import WorkerAgent


class ToolThenTextLLM:
    """Calls a tool until tools are withdrawn, then replays the scripted plain-text replies."""

    model = "stub"

    def __init__(self, *final_replies: str) -> None:
        self.final_replies = list(final_replies)
        self.final_prompts: List[str] = []

    def generate(self, messages: List[Dict[str, Any]], tools: Optional[Any] = None, **kwargs: Any) -> LLMResponse:
        if tools:
            call = ToolCallRequest(id="c1", name="calculator", arguments={"expression": "1 + 1"})
            return LLMResponse(content="", tool_calls=[call])
        self.final_prompts.append(str(messages[-1].get("content", "")))
        return LLMResponse(content=self.final_replies.pop(0))


def _worker(llm: Any, tmp_path: Path, max_steps: int = 2) -> WorkerAgent:
    bus = EventBus()
    return WorkerAgent(
        name="writer",
        role="Writer",
        instructions="Write.",
        llm=llm,
        storage=SQLiteStorage(db_path=str(tmp_path / "s.db")),
        budget=BudgetTracker(config=BudgetConfig(), bus=bus, tenant_id="t"),
        bus=bus,
        tool_names=["calculator"],
        max_steps=max_steps,
    )


@pytest.mark.integration
def test_agent_is_told_when_tools_are_withdrawn(tmp_path: Path) -> None:
    llm = ToolThenTextLLM("The final poem.")
    assert _worker(llm, tmp_path).execute_task("Write", tenant_id="t") == "The final poem."
    assert "final answer" in llm.final_prompts[0].lower()


@pytest.mark.integration
def test_tool_markup_instead_of_answer_gets_one_more_chance(tmp_path: Path) -> None:
    llm = ToolThenTextLLM('<tool_call>{"name": "x"}</tool_call>', "The final poem.")
    assert _worker(llm, tmp_path).execute_task("Write", tenant_id="t") == "The final poem."


@pytest.mark.integration
def test_observations_remain_the_last_resort(tmp_path: Path) -> None:
    llm = ToolThenTextLLM("<tool_call>x</tool_call>", "<tool_call>y</tool_call>")
    assert "[calculator]" in _worker(llm, tmp_path).execute_task("Write", tenant_id="t")


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_hopeless_errors_are_not_retried(status: int) -> None:
    calls: List[int] = []

    def fail() -> None:
        calls.append(1)
        raise _StatusError(status)

    with pytest.raises(_StatusError):
        retry_with_backoff(fail, retry_config=RetryConfig(max_retries=5))
    assert len(calls) == 1


@pytest.mark.unit
def test_long_provider_wait_fails_fast_instead_of_sleeping() -> None:
    calls: List[int] = []

    def daily_quota() -> None:
        calls.append(1)
        raise RuntimeError("429 quota exceeded, retry in 40000s")

    with pytest.raises(RuntimeError, match="quota"):
        retry_with_backoff(daily_quota, retry_config=RetryConfig(max_retries=5))
    assert len(calls) == 1


@pytest.mark.unit
def test_configured_mission_minimums_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CONFIG, "mission_min_steps", 256)
    monkeypatch.setattr(CONFIG, "mission_min_tokens", 2_000_000)
    spec = SwarmSpec.from_dict(
        {
            "swarm_id": "s",
            "title": "t",
            "topology": "single",
            "roles": [{"name": "a", "purpose": "p", "instructions": "i"}],
        },
        enforced_tenant_id="t",
    )
    IntegratedMissionRunner._apply_budget_policy(spec)
    assert spec.budgets.max_swarm_steps == 256
    assert spec.budgets.max_total_tokens == 2_000_000


@pytest.mark.unit
def test_ollama_limits_come_from_config() -> None:
    source = Path("core/llm.py").read_text(encoding="utf-8")
    assert '"num_ctx": CONFIG.ollama_num_ctx' in source and '"num_predict": CONFIG.ollama_num_predict' in source
    assert "timeout=45.0" not in source and "timeout=120" not in source


@pytest.mark.integration
@pytest.mark.parametrize("reply", [
    "The search failed. Let me proceed with the search again.",
    "To convert, add 273.15.\n\nLet me calculate this.",
    "I'll try a different approach.",
])
def test_reply_that_only_announces_more_work_gets_one_more_chance(tmp_path: Path, reply: str) -> None:
    llm = ToolThenTextLLM(reply, "373 K")
    assert _worker(llm, tmp_path).execute_task("Write", tenant_id="t") == "373 K"


@pytest.mark.integration
@pytest.mark.parametrize("reply", ["The answer is 373 K.", "I will always love the sea, the poet wrote. It ends here."])
def test_finished_reply_is_not_retried(tmp_path: Path, reply: str) -> None:
    llm = ToolThenTextLLM(reply)
    assert _worker(llm, tmp_path).execute_task("Write", tenant_id="t") == reply
    assert len(llm.final_prompts) == 1


@pytest.mark.integration
def test_announcement_is_kept_when_the_retry_returns_nothing(tmp_path: Path) -> None:
    llm = ToolThenTextLLM("Let me check.", "<tool_call>x</tool_call>")
    assert _worker(llm, tmp_path).execute_task("Write", tenant_id="t") == "Let me check."


@pytest.mark.unit
def test_refused_run_python_points_to_the_calculator(monkeypatch: pytest.MonkeyPatch) -> None:
    from swarm_tools.python_exec import run_python

    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(CONFIG, "python_sandbox_mode", "auto")
    monkeypatch.setattr(CONFIG, "unsafe_allow_local_exec", False)
    assert "calculator" in run_python("print(1.005 ** 48)")


@pytest.mark.unit
def test_regex_checker_spans_lines() -> None:
    from benchmarks.runner import check_regex

    assert check_regex("1. **Offer**\n2. Acceptance", "(?=.*offer)(?=.*acceptance)")


@pytest.mark.integration
def test_final_pipeline_stage_sees_the_request_and_must_deliver_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from core.factory import DynamicRoleSpec
    from core.topology import PipelineTopology

    prompts: List[str] = []

    class StageWorker:
        def execute_task(self, task_prompt: str, tenant_id: str) -> str:
            prompts.append(task_prompt)
            return f"stage {len(prompts)} output"

    monkeypatch.setattr(PipelineTopology, "_build_worker", lambda self, role, bus: StageWorker())
    roles = [DynamicRoleSpec(name, "Work", "Do it.") for name in ("drafter", "editor")]
    budget = BudgetTracker(config=BudgetConfig())

    result = PipelineTopology(tenant_id="t", roles=roles, llm=None, budget=budget).execute("Plan four weeks")

    assert result == "stage 2 output"
    assert "Original request" not in prompts[0]
    assert "Plan four weeks" in prompts[1] and "stage 1 output" in prompts[1]
    assert "complete deliverable" in prompts[1]
