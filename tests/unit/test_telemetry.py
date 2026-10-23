"""
tests/unit/test_telemetry.py - Unit tests for UsageLedger, Cost Calculation & Telemetry.
"""

import pytest

from core.storage import SQLiteStorage
from core.telemetry import (
    UsageLedger,
    calculate_call_cost,
    current_mission_id,
    current_tenant_id,
)


def test_calculate_call_cost_models():
    # Local Ollama is free
    cost_ollama = calculate_call_cost("qwen3:8b", prompt_tokens=2000, completion_tokens=500)
    assert cost_ollama == 0.0

    # Gemini Flash: $0.075/1M input, $0.30/1M output
    cost_gemini = calculate_call_cost("gemini-2.0-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost_gemini == pytest.approx(0.375, rel=1e-4)

    # GPT-4o with cached tokens: $2.50/1M in, $1.25/1M cached in, $10.00/1M out
    cost_gpt4o = calculate_call_cost(
        "gpt-4o",
        prompt_tokens=1_000_000,
        completion_tokens=100_000,
        cached_prompt_tokens=500_000,
    )
    # (500k * 2.50 + 500k * 1.25 + 100k * 10.00) / 1M = 1.25 + 0.625 + 1.00 = 2.875
    assert cost_gpt4o == pytest.approx(2.875, rel=1e-4)


def test_usage_ledger_record_and_summary():
    ledger = UsageLedger()

    current_mission_id.set("mission_alpha")
    current_tenant_id.set("tenant_test")

    r1 = ledger.record_call(
        agent_name="researcher",
        provider="openai_compatible",
        model="gemini-2.0-flash",
        prompt_tokens=1000,
        completion_tokens=200,
        latency_ms=350.0,
        cached_prompt_tokens=200,
    )
    assert r1.mission_id == "mission_alpha"
    assert r1.tenant_id == "tenant_test"

    ledger.record_call(
        agent_name="coder",
        provider="openai_compatible",
        model="gemini-2.0-flash",
        prompt_tokens=1500,
        completion_tokens=300,
        latency_ms=450.0,
        cached_prompt_tokens=500,
    )

    summary = ledger.get_mission_summary("mission_alpha")
    assert summary["call_count"] == 2
    assert summary["total_prompt_tokens"] == 2500
    assert summary["total_completion_tokens"] == 500
    assert summary["total_cached_prompt_tokens"] == 700
    assert summary["total_tokens"] == 3000
    assert summary["peak_context_tokens"] == 1500
    assert summary["cache_hit_ratio"] == pytest.approx(700 / 2500, rel=1e-4)
    assert summary["total_latency_ms"] == 800.0
    assert summary["total_cost_usd"] > 0.0

    spend = ledger.get_tenant_spend("tenant_test")
    assert spend == summary["total_cost_usd"]


def test_usage_ledger_sqlite_persistence(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    storage = SQLiteStorage(db_path=db_file)
    ledger = UsageLedger(storage=storage)

    ledger.record_call(
        agent_name="tester",
        provider="ollama",
        model="qwen3:8b",
        prompt_tokens=500,
        completion_tokens=100,
        latency_ms=210.5,
        mission_id="m_100",
        step_id="s_1",
        tenant_id="tenant_x",
    )

    records_x = storage.get_usage_records("tenant_x")
    assert len(records_x) == 1
    assert records_x[0]["mission_id"] == "m_100"
    assert records_x[0]["prompt_tokens"] == 500
    assert records_x[0]["latency_ms"] == 210.5

    # Cross-tenant isolation check: tenant_y should see 0 records
    records_y = storage.get_usage_records("tenant_y")
    assert len(records_y) == 0
