"""
tests/unit/test_compare_and_demo.py - Statistics of the paired benchmark, and the offline demo path.
"""

import json
from pathlib import Path
from typing import Any, Dict

import pytest

import demo
from benchmarks.compare import DEFAULT_TASKS, mcnemar_exact_p, run_task, summarize, task_passed


def _row(task_id: str, condition: str, passed: bool, cost: float = 0.01, topology: str = "single") -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "pack": "p",
        "condition": condition,
        "passed": passed,
        "topology": topology,
        "seconds": 1.0,
        "tokens": 10,
        "cost_usd": cost,
        "error": None,
    }


@pytest.mark.unit
def test_mcnemar_exact_values() -> None:
    assert mcnemar_exact_p(0, 0) == 1.0
    assert mcnemar_exact_p(5, 5) == 1.0
    assert mcnemar_exact_p(6, 0) == pytest.approx(0.03125)


@pytest.mark.unit
def test_summary_counts_discordant_pairs_and_cost_per_success() -> None:
    rows = [
        _row("a", "swarm", True, 0.04, "peer"),
        _row("a", "single", False),
        _row("b", "swarm", True, 0.04, "single"),
        _row("b", "single", True),
    ]
    summary = summarize(rows)
    assert summary["swarm_only_passes"] == 1 and summary["single_only_passes"] == 0
    assert summary["swarm"]["cost_per_success_usd"] == pytest.approx(0.04)
    assert summary["single"]["cost_per_success_usd"] == pytest.approx(0.02)
    assert summary["selected_topologies"] == {"peer": 1, "single": 1}


@pytest.mark.unit
def test_unpaired_task_is_not_counted_as_a_pair() -> None:
    assert summarize([_row("a", "swarm", True)])["paired_tasks"] == 0


@pytest.mark.unit
def test_no_successes_has_no_cost_per_success() -> None:
    assert summarize([_row("a", "swarm", False), _row("a", "single", False)])["swarm"]["cost_per_success_usd"] is None


@pytest.mark.unit
def test_unknown_checker_fails_closed() -> None:
    assert task_passed({"checker_type": "vibes"}, "anything") is False


@pytest.mark.unit
def test_eval_tasks_are_well_formed_and_do_not_name_tools() -> None:
    tasks = json.loads(DEFAULT_TASKS.read_text(encoding="utf-8"))
    assert len({task["id"] for task in tasks}) == len(tasks) >= 30
    assert len({task["pack"] for task in tasks}) >= 5
    for task in tasks:
        assert task["checker_type"] in {"numeric", "contains", "regex"}
        assert "run_python" not in task["prompt"] and "calculator" not in task["prompt"]


@pytest.mark.integration
def test_crashing_mission_is_recorded_as_a_failure(tmp_path: Path) -> None:
    class Broken:
        model = "broken"

        def generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("provider down")

    task = {"id": "t", "pack": "p", "prompt": "x", "checker_type": "contains", "expected_substr": "x"}
    row = run_task(task, "swarm", Broken(), tmp_path)  # type: ignore[arg-type]
    assert row["passed"] is False and "provider down" in row["error"]


@pytest.mark.integration
@pytest.mark.parametrize(
    "index,expected,source",
    [
        (0, "single", "task_analysis"),
        (1, "pipeline", "task_analysis"),
        (2, "review_loop", "task_analysis"),
        (3, "orchestrator", "task_analysis"),
        (4, "pipeline", "manual"),
    ],
)
def test_demo_scenarios_select_the_expected_topology(tmp_path: Path, index: int, expected: str, source: str) -> None:
    prompt, manual, plan = demo.SCENARIOS[index]
    result = demo.run_scenario(prompt, manual, plan, tmp_path)
    assert result.status == "COMPLETED"
    assert result.spec["topology"] == expected and result.spec["topology_source"] == source


def _mission_row(task_id, condition, passed=True, error=None):
    return {
        "task_id": task_id, "pack": "p", "condition": condition, "passed": passed, "topology": "single",
        "seconds": 1.0, "tokens": 1, "cost_usd": 0.0, "error": error, "output": "",
    }


def test_comparison_saves_after_each_mission_and_resume_skips_finished_work(tmp_path, monkeypatch):
    import json

    from benchmarks import compare

    calls = []

    def fake_run(task, condition, provider, workdir):
        calls.append((task["id"], condition))
        if len(calls) == 3:
            raise KeyboardInterrupt
        return _mission_row(task["id"], condition)

    monkeypatch.setattr(compare, "run_task", fake_run)
    out = tmp_path / "report.json"
    tasks = [{"id": "A"}, {"id": "B"}]
    try:
        compare.run_comparison(tasks, object(), 100.0, out=out)
    except KeyboardInterrupt:
        pass

    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["complete"] is False
    assert len(saved["results"]) == 2

    calls.clear()
    monkeypatch.setattr(compare, "run_task", lambda t, c, p, w: calls.append((t["id"], c)) or _mission_row(t["id"], c))
    report = compare.run_comparison(tasks, object(), 100.0, compare.load_completed_rows(out), out)

    assert calls == [("B", "swarm"), ("B", "single")]
    assert report["complete"] is True
    assert report["summary"]["paired_tasks"] == 2


def test_resume_reruns_crashed_missions(tmp_path):
    import json

    from benchmarks import compare

    out = tmp_path / "report.json"
    rows = [_mission_row("A", "swarm"), _mission_row("A", "single", passed=False, error="ConnectionError: refused")]
    out.write_text(json.dumps({"results": rows}), encoding="utf-8")

    assert compare.load_completed_rows(out) == rows[:1]
    assert compare.load_completed_rows(tmp_path / "missing.json") == []
