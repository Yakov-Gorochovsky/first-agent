"""
tests/unit/test_router.py - Unit tests for FP-04 Per-Role Model Routing & Provider Policy.
"""

from typing import Any, Dict, List, Optional

import pytest

from core.llm import LLMResponse
from core.router import (
    AllProvidersFailedError,
    ModelPolicyRouter,
    PolicyViolationError,
    RoleRoutingConfig,
    RoutingLLMProvider,
    TenantModelPolicy,
)


class DummyMockProvider:
    """Mock LLMProvider implementing LLMProvider protocol for deterministic testing."""

    def __init__(self, model: str, should_fail: bool = False, fail_message: str = "Provider error") -> None:
        self.model = model
        self.should_fail = should_fail
        self.fail_message = fail_message
        self.call_count = 0

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        self.call_count += 1
        if self.should_fail:
            raise RuntimeError(self.fail_message)
        return LLMResponse(
            content=f"Response from {self.model}",
            tool_calls=[],
            prompt_tokens=50,
            completion_tokens=25,
            latency_ms=10.0,
            cost_usd=0.0005,
        )


def test_primary_routing_success() -> None:
    """Verify standard happy-path routing to primary model and provider."""
    router = ModelPolicyRouter()
    tenant_id = "tenant_alpha"

    # Default permissive policy allows all standard providers
    config = RoleRoutingConfig(
        primary_provider="gemini",
        primary_model="gemini-2.0-flash",
    )

    mock_gemini = DummyMockProvider("gemini-2.0-flash")
    provider_instances = {
        "gemini": lambda m: mock_gemini,
    }

    routing_provider = RoutingLLMProvider(
        tenant_id=tenant_id,
        routing_config=config,
        router=router,
        provider_instances=provider_instances,
    )

    resp = routing_provider.generate([{"role": "user", "content": "Hello"}])
    assert "Response from gemini-2.0-flash" in resp.content
    assert mock_gemini.call_count == 1
    assert router.metrics["fallback_count"] == 0
    assert router.metrics["policy_violation_count"] == 0


def test_ordered_fallback_on_primary_failure() -> None:
    """Verify that when primary provider fails, router cascades to allowed fallback."""
    router = ModelPolicyRouter()
    tenant_id = "tenant_resilient"

    config = RoleRoutingConfig(
        primary_provider="openai",
        primary_model="gpt-4o",
        fallback_chain=[("gemini", "gemini-2.0-flash"), ("ollama", "qwen2.5:3b")],
    )

    failing_openai = DummyMockProvider("gpt-4o", should_fail=True, fail_message="OpenAI 503 Overloaded")
    healthy_gemini = DummyMockProvider("gemini-2.0-flash", should_fail=False)
    healthy_ollama = DummyMockProvider("qwen2.5:3b", should_fail=False)

    provider_instances = {
        "openai": lambda m: failing_openai,
        "gemini": lambda m: healthy_gemini,
        "ollama": lambda m: healthy_ollama,
    }

    routing_provider = RoutingLLMProvider(
        tenant_id=tenant_id,
        routing_config=config,
        router=router,
        provider_instances=provider_instances,
    )

    resp = routing_provider.generate([{"role": "user", "content": "Analyze report"}])
    assert failing_openai.call_count == 1
    assert healthy_gemini.call_count == 1
    assert healthy_ollama.call_count == 0  # didn't need 2nd fallback
    assert "Response from gemini-2.0-flash" in resp.content
    assert router.metrics["fallback_count"] == 1
    assert router.metrics["policy_violation_count"] == 0


def test_data_residency_jurisdiction_enforcement() -> None:
    """Verify that a tenant with strict EU data residency cannot route to US providers."""
    router = ModelPolicyRouter()
    eu_tenant = "eu_banking_corp"

    # Allow providers by name, but strictly restrict jurisdiction to EU and local
    router.register_tenant_policy(
        TenantModelPolicy(
            tenant_id=eu_tenant,
            allowed_jurisdictions={"eu", "local"},
            allowed_providers={"mistral_eu", "ollama", "openai"},
        )
    )

    # Attempt to route to OpenAI (which is US jurisdiction)
    us_config = RoleRoutingConfig(
        primary_provider="openai",
        primary_model="gpt-4o",
    )

    provider_instances = {
        "openai": lambda m: DummyMockProvider("gpt-4o"),
    }

    routing_provider = RoutingLLMProvider(
        tenant_id=eu_tenant,
        routing_config=us_config,
        router=router,
        provider_instances=provider_instances,
    )

    with pytest.raises(PolicyViolationError) as exc_info:
        routing_provider.generate([{"role": "user", "content": "Classified EU citizen data"}])

    assert "jurisdiction 'us' is not permitted" in str(exc_info.value)
    assert router.metrics["policy_violation_count"] == 1


def test_local_only_cloud_inference_prohibition() -> None:
    """Verify that local-only deployments reject any cloud inference calls."""
    router = ModelPolicyRouter()
    airgapped_tenant = "defense_tenant"

    # Allow gemini in allowed_providers, but disallow all cloud inference
    router.register_tenant_policy(
        TenantModelPolicy(
            tenant_id=airgapped_tenant,
            allowed_providers={"ollama", "gemini"},
            allowed_jurisdictions={"local", "us"},
            allow_cloud_inference=False,
        )
    )

    cloud_config = RoleRoutingConfig(
        primary_provider="gemini",
        primary_model="gemini-2.0-flash",
    )

    routing_provider = RoutingLLMProvider(
        tenant_id=airgapped_tenant,
        routing_config=cloud_config,
        router=router,
        provider_instances={"gemini": lambda m: DummyMockProvider("gemini-2.0-flash")},
    )

    with pytest.raises(PolicyViolationError) as exc_info:
        routing_provider.generate([{"role": "user", "content": "Sensitive internal doc"}])

    assert "allow_cloud_inference=False" in str(exc_info.value)
    assert router.metrics["policy_violation_count"] == 1


def test_provider_allowlist_direct_rejection() -> None:
    """Verify direct rejection when a provider is not in allowed_providers."""
    router = ModelPolicyRouter()
    tenant_id = "tenant_limited"

    router.register_tenant_policy(
        TenantModelPolicy(
            tenant_id=tenant_id,
            allowed_providers={"ollama"},
        )
    )

    config = RoleRoutingConfig(
        primary_provider="gemini",
        primary_model="gemini-2.0-flash",
    )

    routing_provider = RoutingLLMProvider(
        tenant_id=tenant_id,
        routing_config=config,
        router=router,
        provider_instances={"gemini": lambda m: DummyMockProvider("gemini-2.0-flash")},
    )

    with pytest.raises(PolicyViolationError) as exc_info:
        routing_provider.generate([{"role": "user", "content": "Hello"}])

    assert "not in tenant allowed_providers" in str(exc_info.value)
    assert router.metrics["policy_violation_count"] == 1


def test_fallback_chain_strictly_respects_tenant_policy() -> None:
    """
    CRITICAL SECURITY INVARIANT:
    When primary fails, fallback must NEVER route to a provider disallowed by tenant policy,
    even if that provider is present in the fallback chain.
    """
    router = ModelPolicyRouter()
    restricted_tenant = "strict_tenant"

    # Tenant allows only local ollama and mistral_eu (EU)
    router.register_tenant_policy(
        TenantModelPolicy(
            tenant_id=restricted_tenant,
            allowed_providers={"ollama", "mistral_eu"},
            allowed_jurisdictions={"local", "eu"},
        )
    )

    # Primary is ollama (allowed). Fallback 1 is openai (DISALLOWED - US). Fallback 2 is mistral_eu (ALLOWED - EU).
    config = RoleRoutingConfig(
        primary_provider="ollama",
        primary_model="qwen2.5:3b",
        fallback_chain=[
            ("openai", "gpt-4o"),         # Forbidden for this tenant!
            ("mistral_eu", "mistral-large"), # Permitted for this tenant!
        ],
    )

    failing_ollama = DummyMockProvider("qwen2.5:3b", should_fail=True, fail_message="Ollama engine down")
    unauthorized_openai = DummyMockProvider("gpt-4o", should_fail=False)
    healthy_mistral = DummyMockProvider("mistral-large", should_fail=False)

    provider_instances = {
        "ollama": lambda m: failing_ollama,
        "openai": lambda m: unauthorized_openai,
        "mistral_eu": lambda m: healthy_mistral,
    }

    routing_provider = RoutingLLMProvider(
        tenant_id=restricted_tenant,
        routing_config=config,
        router=router,
        provider_instances=provider_instances,
    )

    resp = routing_provider.generate([{"role": "user", "content": "Process private task"}])

    # Assert that unauthorized OpenAI was NEVER called!
    assert unauthorized_openai.call_count == 0
    assert failing_ollama.call_count == 1
    assert healthy_mistral.call_count == 1
    assert "Response from mistral-large" in resp.content


def test_all_providers_fail_cleanly() -> None:
    """Verify that when all allowed providers in chain fail, AllProvidersFailedError is raised without leaking."""
    router = ModelPolicyRouter()
    tenant_id = "tenant_outage"

    config = RoleRoutingConfig(
        primary_provider="ollama",
        primary_model="qwen2.5:3b",
        fallback_chain=[("gemini", "gemini-2.0-flash")],
    )

    provider_instances = {
        "ollama": lambda m: DummyMockProvider("qwen2.5:3b", should_fail=True, fail_message="Local daemon dead"),
        "gemini": lambda m: DummyMockProvider("gemini-2.0-flash", should_fail=True, fail_message="Quota 429"),
    }

    routing_provider = RoutingLLMProvider(
        tenant_id=tenant_id,
        routing_config=config,
        router=router,
        provider_instances=provider_instances,
    )

    with pytest.raises(AllProvidersFailedError) as exc_info:
        routing_provider.generate([{"role": "user", "content": "Test all fail"}])

    assert "All allowed LLM providers failed" in str(exc_info.value)
