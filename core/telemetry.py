"""
core/telemetry.py - Usage Ledger, Cost Governance & OpenTelemetry-Compatible Spans.

Part of FP-00 (Evaluation, Telemetry & Measurement Foundation).
Tracks exact per-call and cumulative tokens (prompt, completion, cached), latency, and
calculates USD cost from a versioned pricing catalog.
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("neural_swarm.telemetry")

# Context variables for tracing without contaminating call signatures
current_mission_id: ContextVar[Optional[str]] = ContextVar("current_mission_id", default=None)
current_step_id: ContextVar[Optional[str]] = ContextVar("current_step_id", default=None)
current_tenant_id: ContextVar[Optional[str]] = ContextVar("current_tenant_id", default="default")

PRICING_CATALOG_VERSION = "2026-10-v1"

# Prices per 1,000,000 tokens (USD)
PRICING_TABLE: Dict[str, Dict[str, float]] = {
    # Local models (zero financial API cost)
    "ollama": {"input": 0.0, "output": 0.0, "cached_input": 0.0},
    "mock-deterministic-v1": {"input": 0.0, "output": 0.0, "cached_input": 0.0},
    # Cloud models
    "gemini-2.0-flash": {"input": 0.075, "output": 0.30, "cached_input": 0.01875},
    "gemini-1.5-flash": {"input": 0.075, "output": 0.30, "cached_input": 0.01875},
    "gemini-1.5-pro": {"input": 1.25, "output": 5.00, "cached_input": 0.3125},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached_input": 0.075},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cached_input": 1.25},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00, "cached_input": 0.30},
}


def calculate_call_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_prompt_tokens: int = 0,
) -> float:
    """Calculate USD cost for an LLM turn based on the versioned price table."""
    norm_model = model.lower()
    rates = None
    for k, v in PRICING_TABLE.items():
        if k in norm_model:
            rates = v
            break

    if not rates:
        rates = PRICING_TABLE["ollama"]

    net_input = max(0, prompt_tokens - cached_prompt_tokens)
    cost = (
        (net_input * rates["input"] / 1_000_000.0)
        + (cached_prompt_tokens * rates.get("cached_input", rates["input"]) / 1_000_000.0)
        + (completion_tokens * rates["output"] / 1_000_000.0)
    )
    return round(cost, 8)


@dataclass
class CallUsageRecord:
    call_id: str
    mission_id: str
    step_id: str
    tenant_id: str
    agent_name: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int
    latency_ms: float
    cost_usd: float
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class UsageLedger:
    """
    In-memory and durable SQLite ledger tracking token burns, latencies, and financial costs.
    """

    def __init__(self, storage: Optional[Any] = None) -> None:
        self.storage = storage
        self._records: List[CallUsageRecord] = []

    def record_call(
        self,
        agent_name: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        cached_prompt_tokens: int = 0,
        mission_id: Optional[str] = None,
        step_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> CallUsageRecord:
        m_id = mission_id or current_mission_id.get() or f"m_{uuid.uuid4().hex[:8]}"
        s_id = step_id or current_step_id.get() or f"s_{uuid.uuid4().hex[:6]}"
        t_id = tenant_id or current_tenant_id.get() or "default"

        cost = calculate_call_cost(model, prompt_tokens, completion_tokens, cached_prompt_tokens)

        rec = CallUsageRecord(
            call_id=f"call_{uuid.uuid4().hex[:10]}",
            mission_id=m_id,
            step_id=s_id,
            tenant_id=t_id,
            agent_name=agent_name,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            latency_ms=round(latency_ms, 2),
            cost_usd=cost,
        )
        self._records.append(rec)

        if self.storage and hasattr(self.storage, "save_usage_record"):
            self.storage.save_usage_record(rec)

        logger.info(
            json.dumps({
                "event": "llm_call_usage",
                "call_id": rec.call_id,
                "mission_id": rec.mission_id,
                "tenant_id": rec.tenant_id,
                "agent": rec.agent_name,
                "tokens": {
                    "prompt": rec.prompt_tokens,
                    "completion": rec.completion_tokens,
                    "cached": rec.cached_prompt_tokens,
                    "total": rec.prompt_tokens + rec.completion_tokens,
                },
                "latency_ms": rec.latency_ms,
                "cost_usd": rec.cost_usd,
            })
        )
        return rec

    def get_mission_summary(self, mission_id: str) -> Dict[str, Any]:
        """Aggregate all metrics for a given mission."""
        records = [r for r in self._records if r.mission_id == mission_id]
        if not records:
            return {
                "mission_id": mission_id,
                "call_count": 0,
                "total_prompt_tokens": 0,
                "total_completion_tokens": 0,
                "total_cached_prompt_tokens": 0,
                "total_tokens": 0,
                "peak_context_tokens": 0,
                "total_latency_ms": 0.0,
                "total_cost_usd": 0.0,
                "cache_hit_ratio": 0.0,
            }

        total_p = sum(r.prompt_tokens for r in records)
        total_c = sum(r.completion_tokens for r in records)
        total_cached = sum(r.cached_prompt_tokens for r in records)
        peak_p = max(r.prompt_tokens for r in records)
        total_lat = sum(r.latency_ms for r in records)
        total_cost = sum(r.cost_usd for r in records)
        hit_ratio = (total_cached / total_p) if total_p > 0 else 0.0

        return {
            "mission_id": mission_id,
            "call_count": len(records),
            "total_prompt_tokens": total_p,
            "total_completion_tokens": total_c,
            "total_cached_prompt_tokens": total_cached,
            "total_tokens": total_p + total_c,
            "peak_context_tokens": peak_p,
            "total_latency_ms": round(total_lat, 2),
            "total_cost_usd": round(total_cost, 6),
            "cache_hit_ratio": round(hit_ratio, 4),
        }

    def get_tenant_spend(self, tenant_id: str) -> float:
        """Sum total USD spend for a tenant."""
        return round(sum(r.cost_usd for r in self._records if r.tenant_id == tenant_id), 6)
