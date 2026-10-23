"""
core/router.py - FP-04: Per-Role Model Routing, Data Residency, and Provider Policy.

Architectural Purpose:
Decouples agent personas from monolithic, fixed LLM backends and enforces tenant-level
data residency, provider contractual allow-lists, and resilient ordered fallback chains.

Key Capabilities:
1. Provider Profile & Residency Registry:
   - Tracks data residency jurisdiction ('local', 'us', 'eu', 'apac'), data retention days,
     and cloud vs. local inference attributes for each provider.
2. Tenant Policy Enforcement:
   - Enforces strict allow-lists of approved providers and allowed data jurisdictions.
   - Enforces cloud inference permissions (e.g., local-only deployment guarantees).
   - Zero-tolerance fail-closed policy validation: Disallowed providers are immediately
     blocked and logged as security policy violations.
3. Ordered Fallback Chain:
   - Primary model/provider failures (timeouts, rate limits, circuit breaker trips)
     gracefully cascade to secondary allowed providers in predefined priority order.
   - Fallback candidates are strictly pre-filtered against the tenant's security policy.
     Under no condition can a fallback route to a provider unapproved by the tenant.
4. RoutingLLMProvider (LLMProvider Protocol Adapter):
   - Integrates seamlessly with WorkerAgent and SwarmFactory.
"""

from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.llm import LLMProvider, LLMResponse
from core.storage import validate_tenant_id

logger = logging.getLogger("neural_swarm.router")


class PolicyViolationError(PermissionError):
    """Raised when an LLM call or fallback attempt violates tenant data residency or provider policy."""
    pass


class AllProvidersFailedError(RuntimeError):
    """Raised when primary and all policy-compliant fallback providers fail."""
    pass


@dataclass(frozen=True)
class ProviderProfile:
    """Metadata describing provider data handling, jurisdiction, and privacy terms."""
    provider_id: str
    name: str
    jurisdiction: str  # 'local', 'us', 'eu', 'apac'
    is_cloud: bool
    data_retention_days: int = 0
    training_opt_out_verified: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "jurisdiction": self.jurisdiction,
            "is_cloud": self.is_cloud,
            "data_retention_days": self.data_retention_days,
            "training_opt_out_verified": self.training_opt_out_verified,
        }


# Standard pre-registered provider profiles
BUILTIN_PROVIDERS: Dict[str, ProviderProfile] = {
    "ollama": ProviderProfile(
        provider_id="ollama",
        name="Local Ollama Inference",
        jurisdiction="local",
        is_cloud=False,
        data_retention_days=0,
        training_opt_out_verified=True,
    ),
    "openai": ProviderProfile(
        provider_id="openai",
        name="OpenAI API",
        jurisdiction="us",
        is_cloud=True,
        data_retention_days=30,
        training_opt_out_verified=True,
    ),
    "anthropic": ProviderProfile(
        provider_id="anthropic",
        name="Anthropic Claude API",
        jurisdiction="us",
        is_cloud=True,
        data_retention_days=30,
        training_opt_out_verified=True,
    ),
    "gemini": ProviderProfile(
        provider_id="gemini",
        name="Google Gemini API",
        jurisdiction="us",
        is_cloud=True,
        data_retention_days=0,
        training_opt_out_verified=True,
    ),
    "mistral_eu": ProviderProfile(
        provider_id="mistral_eu",
        name="Mistral AI EU Cloud",
        jurisdiction="eu",
        is_cloud=True,
        data_retention_days=0,
        training_opt_out_verified=True,
    ),
}


@dataclass
class TenantModelPolicy:
    """Data residency, provider allow-lists, and governance bounds for a specific tenant."""
    tenant_id: str
    allowed_providers: Set[str] = field(default_factory=lambda: {"ollama", "openai", "anthropic", "gemini", "mistral_eu"})
    allowed_jurisdictions: Set[str] = field(default_factory=lambda: {"local", "us", "eu", "apac"})
    allow_cloud_inference: bool = True
    max_cost_per_call_usd: float = 1.0


@dataclass
class RoleRoutingConfig:
    """Routing specification for a specific role within a tenant or mission."""
    primary_provider: str
    primary_model: str
    fallback_chain: List[Tuple[str, str]] = field(default_factory=list)  # List of (provider_id, model_name)


class ModelPolicyRouter:
    """
    Evaluates and enforces tenant data residency and provider access policies.
    Guarantees that no model invocation leaves approved jurisdictions or contacts disallowed backends.
    """

    def __init__(
        self,
        provider_profiles: Optional[Dict[str, ProviderProfile]] = None,
    ) -> None:
        self.providers: Dict[str, ProviderProfile] = dict(provider_profiles or BUILTIN_PROVIDERS)
        self._tenant_policies: Dict[str, TenantModelPolicy] = {}
        self._policy_violation_count: int = 0
        self._fallback_count: int = 0
        self._total_calls_dispatched: int = 0

    def register_provider(self, profile: ProviderProfile) -> None:
        """Register or update a provider profile with data residency terms."""
        self.providers[profile.provider_id] = profile

    def register_tenant_policy(self, policy: TenantModelPolicy) -> None:
        """Configure data residency and provider restrictions for a tenant."""
        validate_tenant_id(policy.tenant_id)
        self._tenant_policies[policy.tenant_id] = policy

    def get_tenant_policy(self, tenant_id: str) -> TenantModelPolicy:
        """Retrieve tenant policy, falling back to permissive default if unset."""
        validate_tenant_id(tenant_id)
        return self._tenant_policies.get(tenant_id, TenantModelPolicy(tenant_id=tenant_id))

    def validate_provider_access(self, tenant_id: str, provider_id: str) -> ProviderProfile:
        """
        Verify that a provider satisfies tenant contractual allow-lists and data residency rules.
        Raises PolicyViolationError immediately if any rule is breached.
        """
        validate_tenant_id(tenant_id)
        policy = self.get_tenant_policy(tenant_id)

        profile = self.providers.get(provider_id)
        if not profile:
            self._policy_violation_count += 1
            msg = f"Tenant '{tenant_id}' requested unknown or unregistered provider '{provider_id}'."
            logger.error("[SECURITY POLICY VIOLATION] %s", msg)
            raise PolicyViolationError(msg)

        # 1. Check provider allow-list
        if "*" not in policy.allowed_providers and provider_id not in policy.allowed_providers:
            self._policy_violation_count += 1
            msg = (
                f"Tenant '{tenant_id}' violated provider policy: '{provider_id}' "
                f"is not in tenant allowed_providers ({sorted(policy.allowed_providers)})."
            )
            logger.error("[SECURITY POLICY VIOLATION] %s", msg)
            raise PolicyViolationError(msg)

        # 2. Check cloud inference authorization
        if profile.is_cloud and not policy.allow_cloud_inference:
            self._policy_violation_count += 1
            msg = (
                f"Tenant '{tenant_id}' violated cloud policy: Provider '{provider_id}' is cloud-hosted, "
                f"but tenant policy mandates strictly local inference (allow_cloud_inference=False)."
            )
            logger.error("[SECURITY POLICY VIOLATION] %s", msg)
            raise PolicyViolationError(msg)

        # 3. Check data residency jurisdiction
        if "*" not in policy.allowed_jurisdictions and profile.jurisdiction not in policy.allowed_jurisdictions:
            self._policy_violation_count += 1
            msg = (
                f"Tenant '{tenant_id}' violated data residency policy: Provider '{provider_id}' jurisdiction "
                f"'{profile.jurisdiction}' is not permitted by tenant allow-list ({sorted(policy.allowed_jurisdictions)})."
            )
            logger.error("[SECURITY POLICY VIOLATION] %s", msg)
            raise PolicyViolationError(msg)

        return profile

    def resolve_allowed_routes(
        self,
        tenant_id: str,
        routing_config: RoleRoutingConfig,
    ) -> List[Tuple[str, str]]:
        """
        Produce an ordered execution chain starting with primary, followed by valid fallbacks.
        Primary must be compliant (or raises PolicyViolationError).
        Fallback candidates violating tenant policy are purged so they can never be called.
        """
        # Validate primary
        self.validate_provider_access(tenant_id, routing_config.primary_provider)
        valid_chain: List[Tuple[str, str]] = [(routing_config.primary_provider, routing_config.primary_model)]

        # Validate each fallback candidate against tenant policy
        for fb_provider, fb_model in routing_config.fallback_chain:
            try:
                self.validate_provider_access(tenant_id, fb_provider)
                valid_chain.append((fb_provider, fb_model))
            except PolicyViolationError:
                # Silently omit policy-non-compliant fallbacks from candidate chain
                logger.info(
                    "Skipping fallback (%s, %s) for tenant '%s' due to tenant policy restriction.",
                    fb_provider,
                    fb_model,
                    tenant_id,
                )

        return valid_chain

    @property
    def metrics(self) -> Dict[str, Any]:
        return {
            "total_calls_dispatched": self._total_calls_dispatched,
            "fallback_count": self._fallback_count,
            "policy_violation_count": self._policy_violation_count,
        }


class RoutingLLMProvider:
    """
    LLMProvider adapter implementing policy routing, data residency enforcement,
    and ordered resilient fallback.
    """

    def __init__(
        self,
        tenant_id: str,
        routing_config: RoleRoutingConfig,
        router: ModelPolicyRouter,
        provider_instances: Dict[str, Callable[[str], LLMProvider]],
    ) -> None:
        self.tenant_id = tenant_id
        self.routing_config = routing_config
        self.router = router
        self.provider_instances = provider_instances  # maps provider_id -> factory(model_name) -> LLMProvider
        self.model = routing_config.primary_model

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        """
        Dispatch request to primary model, cascading to approved fallbacks on provider failure.
        Enforces tenant security policy on every attempt.
        """
        # Compute allowed candidate chain (raises PolicyViolationError if primary is illegal)
        candidates = self.router.resolve_allowed_routes(self.tenant_id, self.routing_config)
        self.router._total_calls_dispatched += 1

        last_error: Optional[Exception] = None

        for idx, (provider_id, model_name) in enumerate(candidates):
            # Double check policy at point of dispatch (zero bypass assumption)
            self.router.validate_provider_access(self.tenant_id, provider_id)

            if idx > 0:
                self.router._fallback_count += 1
                logger.warning(
                    "RoutingLLMProvider cascading to fallback #%d (%s:%s) for tenant '%s' due to: %s",
                    idx,
                    provider_id,
                    model_name,
                    self.tenant_id,
                    last_error,
                )

            provider_factory = self.provider_instances.get(provider_id)
            if not provider_factory:
                last_error = RuntimeError(f"Provider factory for '{provider_id}' is not configured.")
                continue

            try:
                provider_client = provider_factory(model_name)
                response = provider_client.generate(messages=messages, tools=tools)
                return response
            except Exception as exc:
                last_error = exc
                logger.error(
                    "Provider '%s' (model '%s') failed during execution: %s",
                    provider_id,
                    model_name,
                    exc,
                )

        raise AllProvidersFailedError(
            f"All allowed LLM providers failed for tenant '{self.tenant_id}'. Last error: {last_error}"
        )
