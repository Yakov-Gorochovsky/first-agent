"""
tests/unit/test_topology.py - FP-12: Pluggable Multi-Agent Topologies Tests.

Tests:
1. TopologyRegistry registration, retrieval, and listing.
2. PipelineTopology execution: sequential chaining where stage i output feeds stage i+1.
3. ReviewLoopTopology execution: iterative generator-critic convergence.
"""

from typing import Any, Dict, List, Optional
import pytest

from core.budget import BudgetConfig, BudgetTracker
from core.llm import LLMProvider, LLMResponse
from core.registry import DynamicRoleSpec
from core.topology import (
    BaseTopology,
    GLOBAL_TOPOLOGY_REGISTRY,
    PipelineTopology,
    ReviewLoopTopology,
    TopologyRegistry,
)


class MockSequenceLLM(LLMProvider):
    """Returns deterministic mock responses for test scenarios."""

    model: str = "mock-sequence"

    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.call_count = 0

    def generate(
        self,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.call_count += 1
        content = self.responses.pop(0) if self.responses else "Default completion."
        return LLMResponse(
            content=content,
            prompt_tokens=10,
            completion_tokens=5,
        )


@pytest.mark.unit
def test_topology_registry_crud() -> None:
    """Registry lists default topologies and supports dynamic registration."""
    reg = TopologyRegistry()
    assert "orchestrator" in reg.list_topologies()
    assert "peer" in reg.list_topologies()
    assert "pipeline" in reg.list_topologies()
    assert "review_loop" in reg.list_topologies()

    class CustomStarTopology(BaseTopology):
        def execute(self, prompt: str) -> str:
            return f"Star executed: {prompt}"

    reg.register("custom_star", CustomStarTopology)
    assert reg.get("custom_star") == CustomStarTopology


@pytest.mark.unit
def test_pipeline_topology_sequential_execution() -> None:
    """Pipeline stages execute sequentially, passing the previous output as prompt to next stage."""
    roles = [
        DynamicRoleSpec(name="stage1_extractor", purpose="Extract", instructions="Extract keywords"),
        DynamicRoleSpec(name="stage2_summarizer", purpose="Summarize", instructions="Summarize keywords"),
    ]

    mock_llm = MockSequenceLLM(["Extracted: [Alpha, Beta]", "Final Summary: Alpha and Beta active."])
    budget = BudgetTracker(config=BudgetConfig())

    pipeline = PipelineTopology(
        tenant_id="tenant_p",
        roles=roles,
        llm=mock_llm,
        budget=budget,
    )

    result = pipeline.execute("Raw document contents")
    assert result == "Final Summary: Alpha and Beta active."
    assert mock_llm.call_count == 2


@pytest.mark.unit
def test_review_loop_topology_convergence() -> None:
    """ReviewLoop iterates until the reviewer returns 'APPROVED'."""
    roles = [
        DynamicRoleSpec(name="generator", purpose="Draft", instructions="Write draft"),
        DynamicRoleSpec(name="reviewer", purpose="Review", instructions="Critique or APPROVE"),
    ]

    # Turn 1: generator drafts "Draft 1"
    # Turn 2: reviewer critiques "Needs more detail"
    # Turn 3: generator refines "Draft 2 with detail"
    # Turn 4: reviewer approves "APPROVED - Excellent quality"
    mock_llm = MockSequenceLLM([
        "Draft 1",
        "Needs more detail on Alpha.",
        "Draft 2 with detail on Alpha.",
        "APPROVED - Excellent quality.",
    ])
    budget = BudgetTracker(config=BudgetConfig())

    review_loop = ReviewLoopTopology(
        tenant_id="tenant_r",
        roles=roles,
        llm=mock_llm,
        budget=budget,
    )

    final_result = review_loop.execute("Write report on Alpha")
    assert "Draft 2 with detail on Alpha." in final_result
    assert mock_llm.call_count == 4
