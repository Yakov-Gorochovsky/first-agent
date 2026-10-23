"""
core/resilience.py - LLM Client Resilience, Circuit Breaker & Structured Correlation Logging.

Provides:
- Exponential backoff with randomized jitter for transient network/LLM errors.
- Three-state Circuit Breaker (CLOSED -> OPEN -> HALF_OPEN) to prevent cascade failures.
- Structured contextual correlation logging for tracing missions, steps, and LLM calls.
"""

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

logger = logging.getLogger("neural_swarm.resilience")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"      # Normal operation: requests pass through
    OPEN = "OPEN"          # Failing: requests fail fast without hitting provider
    HALF_OPEN = "HALF_OPEN"# Testing recovery: limited trial requests allowed


class CircuitBreakerOpenError(Exception):
    """Raised when an operation is attempted while the circuit breaker is OPEN."""
    def __init__(self, message: str, cooldown_remaining: float):
        super().__init__(message)
        self.cooldown_remaining = cooldown_remaining


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5      # Consecutive failures before opening
    recovery_timeout: float = 30.0  # Seconds to stay OPEN before HALF_OPEN probe
    success_threshold: int = 2      # Consecutive successes in HALF_OPEN to close


class CircuitBreaker:
    """
    Thread-safe-ready, deterministic circuit breaker protecting LLM and tool endpoints.
    """

    def __init__(self, name: str, config: Optional[CircuitBreakerConfig] = None) -> None:
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0

    def before_call(self) -> None:
        """Verify circuit status before invoking downstream service."""
        now = time.time()
        if self.state == CircuitState.OPEN:
            elapsed = now - self.last_failure_time
            if elapsed >= self.config.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                self.success_count = 0
                logger.info(
                    json.dumps({
                        "event": "circuit_half_open",
                        "circuit": self.name,
                        "elapsed_sec": round(elapsed, 2),
                    })
                )
            else:
                remaining = self.config.recovery_timeout - elapsed
                raise CircuitBreakerOpenError(
                    f"Circuit '{self.name}' is OPEN. Retry in {remaining:.1f}s",
                    cooldown_remaining=remaining,
                )

    def on_success(self) -> None:
        """Record successful downstream execution."""
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.config.success_threshold:
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
                logger.info(
                    json.dumps({
                        "event": "circuit_closed",
                        "circuit": self.name,
                        "msg": "Circuit recovered successfully.",
                    })
                )
        elif self.state == CircuitState.CLOSED:
            self.failure_count = 0

    def on_failure(self, exc: Exception) -> None:
        """Record downstream execution failure."""
        if extract_retry_delay(exc) is not None:
            # Rate limits are handled by backoff cooldown, do not trip the service breaker
            return
        self.last_failure_time = time.time()
        self.failure_count += 1

        if self.state == CircuitState.HALF_OPEN or self.failure_count >= self.config.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                json.dumps({
                    "event": "circuit_tripped_open",
                    "circuit": self.name,
                    "failure_count": self.failure_count,
                    "reason": str(exc),
                })
            )

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Wrap a callable within circuit breaker lifecycle."""
        self.before_call()
        try:
            result = fn(*args, **kwargs)
            self.on_success()
            return result
        except Exception as exc:
            self.on_failure(exc)
            raise


@dataclass
class RetryConfig:
    max_retries: int = 5
    initial_delay: float = 1.0
    backoff_factor: float = 2.0
    max_delay: float = 45.0
    jitter: bool = True


def extract_retry_delay(exc: Exception) -> Optional[float]:
    """
    Extract recommended retry delay (in seconds) from RateLimitError, HTTP 429 response,
    headers, or error message body.
    """
    # 1. Check HTTP response headers (e.g. Retry-After)
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None)
        if headers:
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
            if retry_after:
                try:
                    return float(retry_after)
                except ValueError:
                    pass

    # 2. Check error message / body for 'retryDelay' or 'Please retry in X.Xs'
    exc_str = str(exc)
    m = re.search(
        r"(?:retry(?:Delay|_delay)?|retry in)[:\s'\"\\]*([0-9]+(?:\.[0-9]+)?)s?",
        exc_str,
        re.IGNORECASE,
    )
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass

    # 3. If it is a 429 RateLimitError or contains "429" / "rate_limit" / "RESOURCE_EXHAUSTED"
    if "429" in exc_str or "rate limit" in exc_str.lower() or "resource_exhausted" in exc_str.lower():
        return 15.0

    return None


def retry_with_backoff(
    fn: Callable[..., T],
    retry_config: Optional[RetryConfig] = None,
    retryable_exceptions: tuple = (Exception,),
    circuit_breaker: Optional[CircuitBreaker] = None,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> T:
    """
    Execute a callable with exponential backoff, jitter, rate-limit awareness, and optional circuit breaker.
    """
    cfg = retry_config or RetryConfig()
    delay = cfg.initial_delay
    last_exc: Optional[Exception] = None

    for attempt in range(1, cfg.max_retries + 1):
        try:
            if circuit_breaker:
                return circuit_breaker.call(fn)
            return fn()
        except CircuitBreakerOpenError:
            # Do not retry if circuit is open - fail fast
            raise
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == cfg.max_retries:
                logger.error(
                    json.dumps({
                        "event": "retry_exhausted",
                        "attempt": attempt,
                        "max_retries": cfg.max_retries,
                        "error": str(exc),
                    })
                )
                raise exc

            # Calculate sleep delay: prioritize provider-instructed retryDelay on rate limits
            suggested_delay = extract_retry_delay(exc)
            if suggested_delay is not None:
                actual_delay = max(suggested_delay + 1.0, 1.0)
            elif cfg.jitter:
                actual_delay = random.uniform(0, delay)
            else:
                actual_delay = delay

            if on_retry:
                on_retry(attempt, exc, actual_delay)

            logger.warning(
                json.dumps({
                    "event": "retry_backoff",
                    "attempt": attempt,
                    "delay_sec": round(actual_delay, 3),
                    "error": str(exc),
                })
            )

            time.sleep(actual_delay)
            delay = min(delay * cfg.backoff_factor, cfg.max_delay)

    if last_exc:
        raise last_exc
    raise RuntimeError("Unexpected retry loop termination")
