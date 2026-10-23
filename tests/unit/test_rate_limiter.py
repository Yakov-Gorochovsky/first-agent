"""
tests/unit/test_rate_limiter.py - FP-03: Rate Limiter, Concurrency Metering, & Budget Enforcement Tests.

Tests:
1. TokenBucket refill rate, burst tolerance, and exhaustion.
2. TenantCostGovernor multi-tenant isolation, rate limit enforcement, and concurrency caps.
3. Daily spend cap calculation based on SQLite usage ledger records.
4. BudgetTracker mission dollar cap enforcement and bounded completion tokens.
"""

import time

import pytest

from core.budget import BudgetConfig, BudgetExceededError, BudgetTracker
from core.rate_limiter import (
    ConcurrencyLimitExceededError,
    DailyBudgetExceededError,
    RateLimitExceededError,
    TenantCostGovernor,
    TokenBucket,
)
from core.storage import SQLiteStorage


@pytest.mark.unit
def test_token_bucket_burst_and_exhaustion() -> None:
    """Bucket allows burst up to capacity then refuses further requests until refilled."""
    bucket = TokenBucket(capacity=3.0, refill_rate_per_sec=10.0)

    # Acquire 3 immediate tokens (burst)
    assert bucket.acquire(1.0) is True
    assert bucket.acquire(1.0) is True
    assert bucket.acquire(1.0) is True

    # 4th immediate token fails
    assert bucket.acquire(1.0) is False
    assert bucket.time_until_available(1.0) > 0.0

    # Sleep briefly to refill
    time.sleep(0.15)
    assert bucket.acquire(1.0) is True


@pytest.mark.unit
def test_tenant_cost_governor_rate_limiting() -> None:
    """Governor enforces rate limits per tenant independently."""
    gov = TenantCostGovernor(default_rpm=60.0, default_burst=2.0)

    # Tenant A acquires 2 tokens (its burst)
    gov.check_rate_limit("tenant_a", 1.0)
    gov.check_rate_limit("tenant_a", 1.0)

    # Tenant A fails 3rd request
    with pytest.raises(RateLimitExceededError) as exc:
        gov.check_rate_limit("tenant_a", 1.0)
    assert exc.value.retry_after > 0.0

    # Tenant B is completely unaffected (tenant isolation)
    gov.check_rate_limit("tenant_b", 1.0)


@pytest.mark.unit
def test_tenant_cost_governor_concurrency_tracking() -> None:
    """Governor enforces active concurrent mission limits per tenant."""
    gov = TenantCostGovernor(max_concurrency_per_tenant=2)

    with gov.track_concurrency("tenant_c") as c1:
        assert c1 == 1
        with gov.track_concurrency("tenant_c") as c2:
            assert c2 == 2
            # 3rd concurrent run fails
            with pytest.raises(ConcurrencyLimitExceededError) as exc:
                with gov.track_concurrency("tenant_c"):
                    pass
            assert exc.value.current == 2
            assert exc.value.max_allowed == 2

        # After releasing one, another can enter
        with gov.track_concurrency("tenant_c") as c3:
            assert c3 == 2


@pytest.mark.unit
def test_tenant_cost_governor_daily_spend_cap(tmp_path) -> None:
    """Governor aggregates rolling 24h spend from storage and blocks when cap is exceeded."""
    db_file = tmp_path / "test_spend.db"
    storage = SQLiteStorage(db_path=str(db_file))

    gov = TenantCostGovernor(storage=storage, max_daily_usd_per_tenant=5.0)

    # Spend $2.00 (within limit)
    storage.save_usage_record(
        {
            "call_id": "c1",
            "mission_id": "m1",
            "step_id": "s1",
            "agent_name": "agent1",
            "tenant_id": "tenant_d",
            "provider": "google",
            "model": "gemini-1.5-pro",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 150_000,
            "cached_prompt_tokens": 0,
            "cost_usd": 2.00,
            "latency_ms": 100.0,
            "created_at": time.time(),
        }
    )
    spend = gov.check_daily_spend("tenant_d")
    assert spend == 2.00

    # Spend another $3.50 (total $5.50 > $5.00)
    storage.save_usage_record(
        {
            "call_id": "c2",
            "mission_id": "m2",
            "step_id": "s1",
            "agent_name": "agent1",
            "tenant_id": "tenant_d",
            "provider": "google",
            "model": "gemini-1.5-pro",
            "prompt_tokens": 1_000_000,
            "completion_tokens": 450_000,
            "cached_prompt_tokens": 0,
            "cost_usd": 3.50,
            "latency_ms": 100.0,
            "created_at": time.time(),
        }
    )

    with pytest.raises(DailyBudgetExceededError) as exc:
        gov.check_daily_spend("tenant_d")
    assert exc.value.current_spend == 5.50
    assert exc.value.max_spend == 5.00


@pytest.mark.unit
def test_budget_tracker_cost_and_completion_bounds() -> None:
    """BudgetTracker calculates USD spend, trips on max_mission_cost_usd, and bounds completion tokens."""
    config = BudgetConfig(
        max_mission_cost_usd=0.05,
        hard_max_completion_tokens=500,
    )
    tracker = BudgetTracker(config=config)

    # Check bounded completion tokens
    assert tracker.get_bounded_completion_tokens(100) == 100
    assert tracker.get_bounded_completion_tokens(2000) == 500  # Clamped to hard cap
    assert tracker.get_bounded_completion_tokens(None) == 500

    # Record tokens on cloud model: 10,000 prompt, 2,000 completion on gpt-4o ($0.025 + $0.02 = $0.045)
    tracker.record_tokens("analyst", 10_000, 2_000, model_name="gpt-4o")
    assert tracker.current_cost_usd == 0.045

    # Exceed budget on next call
    with pytest.raises(BudgetExceededError) as exc:
        tracker.record_tokens("analyst", 5_000, 1_000, model_name="gpt-4o")
    assert "Mission cost budget exceeded" in str(exc.value)

    # Check summary contains dollar metric
    summ = tracker.summary()
    assert "cost_usd" in summ
    assert "$0.05" in str(summ["cost_usd"])
