"""
tests/unit/test_free_tier_pacing.py - Free-tier rate limits: pacing, daily quota, and benchmark handling.

A rate-limited mission says nothing about the task, so the benchmark must neither score it nor lose it.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from benchmarks import compare
from core.resilience import (
    RequestPacer,
    RetryConfig,
    is_daily_quota_error,
    is_rate_limit_error,
    retry_with_backoff,
    shared_pacer,
)

GEMINI_DAILY = (
    "Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current quota.', "
    "'status': 'RESOURCE_EXHAUSTED', 'details': [{'violations': [{'quotaId': "
    "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}, {'retryDelay': '20s'}]}}]"
)
GEMINI_MINUTE = (
    "Error code: 429 - [{'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED', 'details': [{'violations': "
    "[{'quotaId': 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}, {'retryDelay': '20s'}]}}]"
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: List[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.unit
def test_pacer_spaces_requests_by_the_minimum_interval() -> None:
    fake = FakeClock()
    pacer = RequestPacer(6.0, clock=fake.clock, sleep=fake.sleep)
    pacer.wait()
    pacer.wait()
    fake.now += 10.0  # Idle longer than the interval: no wait needed.
    pacer.wait()
    assert fake.slept == [6.0]


@pytest.mark.unit
def test_pacer_with_zero_interval_never_sleeps() -> None:
    fake = FakeClock()
    pacer = RequestPacer(0.0, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        pacer.wait()
    assert fake.slept == []


@pytest.mark.unit
def test_shared_pacer_is_one_per_endpoint() -> None:
    a = shared_pacer("https://example.test/v1", 4.0)
    assert shared_pacer("https://example.test/v1", 4.0) is a
    assert shared_pacer("https://other.test/v1", 4.0) is not a


@pytest.mark.unit
def test_daily_quota_is_told_apart_from_per_minute_limits() -> None:
    assert is_rate_limit_error(Exception(GEMINI_DAILY)) and is_daily_quota_error(Exception(GEMINI_DAILY))
    assert is_rate_limit_error(Exception(GEMINI_MINUTE)) and not is_daily_quota_error(Exception(GEMINI_MINUTE))
    assert not is_rate_limit_error(ValueError("bad json"))
    assert not is_daily_quota_error(ValueError("daily report failed"))


@pytest.mark.unit
def test_429_inside_an_ordinary_number_is_not_a_rate_limit() -> None:
    err = ValueError("Error code: 400 - maximum context length 8192, you requested 14290 tokens")
    assert not is_rate_limit_error(err)
    assert not is_daily_quota_error(ValueError("Error code: 400 - daily summary had 14290 rows"))
    assert is_rate_limit_error(Exception("HTTP 429"))


@pytest.mark.unit
def test_retry_gives_up_at_once_on_daily_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.resilience.time.sleep", lambda s: pytest.fail("must not sleep"))
    calls = []

    def fn() -> None:
        calls.append(1)
        raise RuntimeError(GEMINI_DAILY)

    with pytest.raises(RuntimeError):
        retry_with_backoff(fn, retry_config=RetryConfig(max_retries=5))
    assert len(calls) == 1


@pytest.mark.unit
def test_retry_waits_out_a_per_minute_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: List[float] = []
    monkeypatch.setattr("core.resilience.time.sleep", slept.append)
    calls = []

    def fn() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(GEMINI_MINUTE)
        return "ok"

    assert retry_with_backoff(fn, retry_config=RetryConfig(max_retries=5)) == "ok"
    assert slept == [21.0, 21.0]  # Provider's retryDelay plus one second.


# --- Benchmark handling ---------------------------------------------------------------------------

TASK = {"id": "t", "pack": "p", "prompt": "x", "checker_type": "contains", "expected_substr": "x"}


class QuotaProvider:
    """Raises a rate-limit error that the mission code swallows, then answers normally."""

    model = "fake"

    def __init__(self, message: str) -> None:
        self.message = message

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(self.message)


@pytest.mark.unit
def test_run_task_flags_a_rate_limited_mission(tmp_path: Path) -> None:
    row = compare.run_task(TASK, "swarm", QuotaProvider(GEMINI_DAILY), tmp_path)  # type: ignore[arg-type]
    assert row["rate_limited"] == "daily" and row["passed"] is False

    row = compare.run_task(TASK, "swarm", QuotaProvider(GEMINI_MINUTE), tmp_path)  # type: ignore[arg-type]
    assert row["rate_limited"] == "minute"


def _row(task_id: str, condition: str, rate_limited: Any = None) -> Dict[str, Any]:
    return {
        "task_id": task_id, "pack": "p", "condition": condition, "passed": rate_limited is None,
        "topology": "single", "seconds": 1.0, "tokens": 1, "cost_usd": 0.0,
        "error": "RateLimited" if rate_limited else None, "output": "", "rate_limited": rate_limited,
    }


@pytest.mark.unit
def test_daily_quota_stops_the_run_without_scoring_the_mission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(task: Dict[str, Any], condition: str, provider: Any, workdir: Path) -> Dict[str, Any]:
        return _row(task["id"], condition, rate_limited="daily" if task["id"] == "B" else None)

    monkeypatch.setattr(compare, "run_task", fake_run)
    out = tmp_path / "report.json"
    report = compare.run_comparison([{"id": "A"}, {"id": "B"}], object(), 100.0, out=out, sleep=lambda s: None)

    assert [(r["task_id"], r["condition"]) for r in report["results"]] == [("A", "swarm"), ("A", "single")]
    assert report["complete"] is False and report["stopped_reason"] == "daily_quota"
    assert json.loads(out.read_text(encoding="utf-8"))["stopped_reason"] == "daily_quota"


@pytest.mark.unit
def test_daily_quota_waits_and_retries_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: List[str] = []

    def fake_run(task: Dict[str, Any], condition: str, provider: Any, workdir: Path) -> Dict[str, Any]:
        attempts.append(condition)
        return _row(task["id"], condition, rate_limited="daily" if len(attempts) == 1 else None)

    monkeypatch.setattr(compare, "run_task", fake_run)
    slept: List[float] = []
    report = compare.run_comparison(
        [{"id": "A"}], object(), 100.0, sleep=slept.append, quota_wait_sec=3600.0, wait_on_quota=True
    )
    assert attempts == ["swarm", "swarm", "single"]
    assert slept == [3600.0] and report["complete"] is True


@pytest.mark.unit
def test_per_minute_limit_cools_down_then_gives_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compare, "run_task", lambda t, c, p, w: _row(t["id"], c, rate_limited="minute"))
    slept: List[float] = []
    report = compare.run_comparison([{"id": "A"}], object(), 100.0, sleep=slept.append, cooldown_sec=300.0)
    assert slept == [300.0] * compare.MAX_RATE_LIMIT_RETRIES
    assert report["results"] == [] and report["stopped_reason"] == "rate_limited"
