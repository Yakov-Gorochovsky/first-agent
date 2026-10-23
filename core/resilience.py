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
from typing import Any, Callable, Dict, Optional, TypeVar

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


def extract_retry_delay(exc: Exception, attempt: int = 1) -> Optional[float]:
    """
    Universally extract recommended retry delay (in seconds) from any rate limit error:
    - Internal RateLimitExceededError / RateLimitError attributes (exc.retry_after)
    - HTTP response headers (Retry-After in seconds or HTTP date, retry-after-ms, x-ratelimit-reset-*)
    - JSON error response bodies (OpenAI, Anthropic, Google, Groq, Cloudflare, etc.)
    - Text patterns in error messages across all major model and web providers (seconds & milliseconds)
    - Fallback progressive backoff for generic HTTP 429 / Rate Limit errors without explicit timestamps
    """
    # 0. Direct attribute on exception (e.g. exc.retry_after, exc.retry_delay)
    for attr in ("retry_after", "retry_delay", "retryAfter"):
        direct_attr = getattr(exc, attr, None)
        if direct_attr is not None:
            try:
                return float(direct_attr)
            except (ValueError, TypeError):
                pass

    # 1. HTTP Response object or headers dictionary attached to exception
    response = getattr(exc, "response", None)
    headers = None
    if response is not None:
        headers = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(exc, "headers", None)

    if headers and hasattr(headers, "get"):
        # 1a. Standard Retry-After header (seconds or HTTP-date)
        retry_after = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.5, float(retry_after))
            except ValueError:
                # Attempt to parse HTTP Date format (RFC 2822 / 1123)
                try:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(str(retry_after))
                    diff = dt.timestamp() - time.time()
                    if diff > 0:
                        return max(0.5, diff)
                except Exception:
                    pass

        # 1b. retry-after-ms (milliseconds used by Azure, OpenAI, Cloudflare)
        retry_ms = headers.get("retry-after-ms") or headers.get("Retry-After-Ms")
        if retry_ms:
            try:
                return max(0.5, float(retry_ms) / 1000.0)
            except ValueError:
                pass

        # 1c. x-ratelimit-reset-requests, x-ratelimit-reset-tokens, x-ratelimit-reset, ratelimit-reset
        for h_key in (
            "x-ratelimit-reset-requests",
            "x-ratelimit-reset-tokens",
            "x-ratelimit-reset",
            "ratelimit-reset",
        ):
            h_val = headers.get(h_key)
            if h_val:
                val_str = str(h_val).strip()
                if val_str.endswith("ms"):
                    try:
                        return max(0.5, float(val_str[:-2]) / 1000.0)
                    except ValueError:
                        pass
                elif val_str.endswith("s"):
                    try:
                        return max(0.5, float(val_str[:-1]))
                    except ValueError:
                        pass
                else:
                    try:
                        val_num = float(val_str)
                        if val_num > 1_000_000_000:  # Epoch timestamp
                            return max(1.0, val_num - time.time())
                        return max(0.5, val_num)
                    except ValueError:
                        pass

    # 2. Check JSON error body if present (e.g. OpenAI body dict or body string)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        raw_err = body.get("error")
        err_dict: Dict[str, Any] = raw_err if isinstance(raw_err, dict) else body
        for k in ("retry_after", "retryDelay", "retry_delay", "retryAfter"):
            b_delay = err_dict.get(k)
            if b_delay is not None:
                try:
                    return max(0.5, float(b_delay))
                except (ValueError, TypeError):
                    pass

    # 3. Comprehensive Regex matching across error strings and bodies
    exc_str = str(exc)
    if body and not isinstance(body, dict):
        exc_str += " " + str(body)

    # 3a. Millisecond durations: e.g. "try again in 500ms", "retry in 800ms"
    ms_match = re.search(
        r"(?:retry(?:Delay|_delay)?|try again in|retry in)[:\s'\"\\]*([0-9]+(?:\.[0-9]+)?)\s*ms",
        exc_str,
        re.IGNORECASE,
    )
    if ms_match:
        try:
            return max(0.5, float(ms_match.group(1)) / 1000.0)
        except ValueError:
            pass

    # 3b. Seconds durations: e.g. "Please retry in 21.7s", "try again in 5 seconds", "retry after 10s", "resets in 8s"
    s_match = re.search(
        r"(?:retry(?:Delay|_delay)?|try again in|retry in|retry after|resets? in)[:\s'\"\\]*([0-9]+(?:\.[0-9]+)?)\s*(?:s|sec|seconds)?",
        exc_str,
        re.IGNORECASE,
    )
    if s_match:
        try:
            return max(0.5, float(s_match.group(1)))
        except ValueError:
            pass

    # 4. Universal rate limit indicators without explicit timestamp (HTTP 429, Quota, TPM/RPM limit)
    status_code = getattr(exc, "status_code", None) or getattr(response, "status_code", None)
    err_code = getattr(exc, "code", None)
    is_rate_limit = (
        status_code == 429
        or err_code in (429, "429", "rate_limit_exceeded")
        or "429" in exc_str
        or "rate limit" in exc_str.lower()
        or "ratelimit" in exc_str.lower()
        or "resource_exhausted" in exc_str.lower()
        or "quota exceeded" in exc_str.lower()
        or "too many requests" in exc_str.lower()
        or "tpm limit" in exc_str.lower()
        or "rpm limit" in exc_str.lower()
    )

    if is_rate_limit:
        # Step-wise progressive cooldown for rate limits without explicit delay:
        # attempt 1: 5s, attempt 2: 12s, attempt 3: 25s, attempt 4+: 45s
        backoffs = [5.0, 12.0, 25.0, 45.0]
        idx = min(max(attempt - 1, 0), len(backoffs) - 1)
        return backoffs[idx]

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
            suggested_delay = extract_retry_delay(exc, attempt=attempt)
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
