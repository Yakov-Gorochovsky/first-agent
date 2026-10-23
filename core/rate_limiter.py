"""
core/rate_limiter.py - FP-03: Token-Bucket Rate Limiter, Concurrency Metering & Daily Spend Caps.

Provides:
- Thread-safe Token-Bucket rate limiter per tenant preventing traffic floods and noisy-neighbor issues.
- Active mission concurrency counters preventing parallel execution runaway.
- Rolling 24-hour cost governor validating spend against hard dollar limits.
"""

import json
import logging
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional

from core.storage import StorageBackend

logger = logging.getLogger("neural_swarm.governance")


class RateLimitExceededError(RuntimeError):
    """Raised when a tenant exceeds request-per-minute (RPM) or burst thresholds."""
    def __init__(self, message: str, retry_after: float = 1.0):
        super().__init__(message)
        self.retry_after = retry_after


class ConcurrencyLimitExceededError(RuntimeError):
    """Raised when a tenant exceeds active concurrent mission executions."""
    def __init__(self, message: str, current: int, max_allowed: int):
        super().__init__(message)
        self.current = current
        self.max_allowed = max_allowed


class DailyBudgetExceededError(RuntimeError):
    """Raised when a tenant exceeds its rolling 24-hour financial spend ceiling."""
    def __init__(self, message: str, current_spend: float, max_spend: float):
        super().__init__(message)
        self.current_spend = current_spend
        self.max_spend = max_spend


class TokenBucket:
    """
    Standard token-bucket implementation allowing controlled bursts and steady refill.
    """

    def __init__(self, capacity: float, refill_rate_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate_per_sec)
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_update = now

    def acquire(self, tokens: float = 1.0) -> bool:
        """Attempt to consume tokens. Returns True if consumed, False if insufficient."""
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def time_until_available(self, tokens: float = 1.0) -> float:
        """Calculate wait seconds until specified tokens are available."""
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                return 0.0
            deficit = tokens - self.tokens
            return max(0.0, deficit / self.refill_rate)


class TenantCostGovernor:
    """
    Coordinates multi-tenant rate limits, concurrency caps, and 24-hour spend ceilings.
    """

    def __init__(
        self,
        storage: Optional[StorageBackend] = None,
        default_rpm: float = 60.0,
        default_burst: float = 10.0,
        max_concurrency_per_tenant: int = 3,
        max_daily_usd_per_tenant: float = 15.0,
    ) -> None:
        self.storage = storage
        self.default_rpm = default_rpm
        self.default_burst = default_burst
        self.max_concurrency = max_concurrency_per_tenant
        self.max_daily_usd = max_daily_usd_per_tenant

        self._buckets: Dict[str, TokenBucket] = {}
        self._active_concurrency: Dict[str, int] = {}
        self._lock = threading.Lock()

    def _get_bucket(self, tenant_id: str) -> TokenBucket:
        with self._lock:
            if tenant_id not in self._buckets:
                refill_rate = self.default_rpm / 60.0
                self._buckets[tenant_id] = TokenBucket(
                    capacity=self.default_burst,
                    refill_rate_per_sec=refill_rate,
                )
            return self._buckets[tenant_id]

    def check_rate_limit(self, tenant_id: str, tokens: float = 1.0) -> None:
        """Verify token bucket limit for tenant."""
        bucket = self._get_bucket(tenant_id)
        if not bucket.acquire(tokens):
            wait_time = bucket.time_until_available(tokens)
            logger.warning(
                json.dumps({
                    "event": "rate_limit_exceeded",
                    "tenant_id": tenant_id,
                    "retry_after": round(wait_time, 2),
                })
            )
            raise RateLimitExceededError(
                f"Rate limit exceeded for tenant '{tenant_id}'. Retry in {wait_time:.1f}s",
                retry_after=wait_time,
            )

    def check_daily_spend(self, tenant_id: str) -> float:
        """Verify rolling 24h spend against hard dollar cap."""
        if not self.storage or not hasattr(self.storage, "get_usage_records"):
            return 0.0

        records = self.storage.get_usage_records(tenant_id, limit=500)
        cutoff = time.time() - 86400.0  # 24 hours ago
        recent_spend = sum(
            float(r.get("cost_usd", 0.0))
            for r in records
            if float(r.get("created_at", 0.0)) >= cutoff
        )
        recent_spend = round(recent_spend, 4)

        if recent_spend >= self.max_daily_usd:
            logger.error(
                json.dumps({
                    "event": "daily_spend_cap_exceeded",
                    "tenant_id": tenant_id,
                    "current_spend": recent_spend,
                    "max_daily_usd": self.max_daily_usd,
                })
            )
            raise DailyBudgetExceededError(
                f"Daily spend cap exceeded for tenant '{tenant_id}' (${recent_spend:.2f} >= ${self.max_daily_usd:.2f}).",
                current_spend=recent_spend,
                max_spend=self.max_daily_usd,
            )
        return recent_spend

    @contextmanager
    def track_concurrency(self, tenant_id: str) -> Iterator[int]:
        """Track and enforce concurrent active missions for a tenant."""
        with self._lock:
            current = self._active_concurrency.get(tenant_id, 0)
            if current >= self.max_concurrency:
                logger.warning(
                    json.dumps({
                        "event": "concurrency_limit_exceeded",
                        "tenant_id": tenant_id,
                        "current": current,
                        "max": self.max_concurrency,
                    })
                )
                raise ConcurrencyLimitExceededError(
                    f"Concurrency limit exceeded for tenant '{tenant_id}' ({current}/{self.max_concurrency} active runs).",
                    current=current,
                    max_allowed=self.max_concurrency,
                )
            self._active_concurrency[tenant_id] = current + 1
            active_count = self._active_concurrency[tenant_id]

        try:
            yield active_count
        finally:
            with self._lock:
                if tenant_id in self._active_concurrency:
                    self._active_concurrency[tenant_id] = max(0, self._active_concurrency[tenant_id] - 1)
