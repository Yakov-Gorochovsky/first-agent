"""
tests/e2e/test_smoke_benchmark.py - Continuous Integration check for FP-00 Smoke Benchmark.
"""

from pathlib import Path
from benchmarks.runner import SmokeBenchmarkRunner
from core.llm import LLMResponse, ToolCallRequest


class BenchmarkMockLLM:
    """Mock LLM to run benchmark in deterministic offline mode during pytest."""

    def __init__(self):
        self.model = "mock-smoke-v0"

    def generate(self, messages, tools=None):
        return LLMResponse(content="Mock benchmark answer.", prompt_tokens=10, completion_tokens=5)


def test_smoke_benchmark_e2e(tmp_path):
    mock_llm = BenchmarkMockLLM()
    runner = SmokeBenchmarkRunner(provider=mock_llm, tenant_id="ci_smoke_tenant")
    report = runner.run_all()

    assert report["benchmark_tier"] == "smoke_v0"
    assert report["summary"]["total_tasks"] == 10
    assert report["summary"]["passed_tasks"] == 10
    assert report["summary"]["pass_rate"] == 1.0
    assert report["config_hash"] != ""
