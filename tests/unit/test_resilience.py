"""
tests/unit/test_resilience.py - Unit tests for LLM Client Resilience & Circuit Breaker.
"""

import time
import pytest
from core.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
    RetryConfig,
    retry_with_backoff,
)


def test_circuit_breaker_success_flow():
    cb = CircuitBreaker("test_cb", CircuitBreakerConfig(failure_threshold=3, recovery_timeout=0.1))
    assert cb.state == CircuitState.CLOSED

    res = cb.call(lambda x, y: x + y, 2, 3)
    assert res == 5
    assert cb.failure_count == 0
    assert cb.state == CircuitState.CLOSED


def test_circuit_breaker_trip_and_fast_fail():
    cb = CircuitBreaker("test_trip", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=0.2))

    def failing_call():
        raise ValueError("Simulated network timeout")

    with pytest.raises(ValueError):
        cb.call(failing_call)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 1

    with pytest.raises(ValueError):
        cb.call(failing_call)
    assert cb.state == CircuitState.OPEN
    assert cb.failure_count == 2

    # Third call should immediately fail fast with CircuitBreakerOpenError (no execution)
    called = False
    def should_not_run():
        nonlocal called
        called = True

    with pytest.raises(CircuitBreakerOpenError) as exc_info:
        cb.call(should_not_run)
    assert not called
    assert exc_info.value.cooldown_remaining > 0


def test_circuit_breaker_half_open_recovery():
    cb = CircuitBreaker(
        "test_recovery",
        CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05, success_threshold=2),
    )

    # Trip the circuit
    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("Trip")))
    assert cb.state == CircuitState.OPEN

    # Wait for recovery timeout
    time.sleep(0.06)

    # First probe success in HALF_OPEN
    val1 = cb.call(lambda: "probe_1")
    assert val1 == "probe_1"
    assert cb.state == CircuitState.HALF_OPEN
    assert cb.success_count == 1

    # Second probe success should close the circuit
    val2 = cb.call(lambda: "probe_2")
    assert val2 == "probe_2"
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_circuit_breaker_half_open_failure_reopens():
    cb = CircuitBreaker(
        "test_reopen",
        CircuitBreakerConfig(failure_threshold=1, recovery_timeout=0.05, success_threshold=2),
    )
    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("Trip")))
    assert cb.state == CircuitState.OPEN

    time.sleep(0.06)
    # Probe fails -> immediately re-opens
    with pytest.raises(RuntimeError):
        cb.call(lambda: (_ for _ in ()).throw(RuntimeError("Probe Failed")))
    assert cb.state == CircuitState.OPEN


def test_retry_with_backoff_eventual_success():
    attempts = 0

    def flaky_func():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionError(f"Temporary drop #{attempts}")
        return "success_payload"

    cfg = RetryConfig(max_retries=3, initial_delay=0.01, backoff_factor=1.5, jitter=False)
    retries_recorded = []

    def record_retry(att, exc, delay):
        retries_recorded.append((att, str(exc)))

    res = retry_with_backoff(
        flaky_func,
        retry_config=cfg,
        retryable_exceptions=(ConnectionError,),
        on_retry=record_retry,
    )
    assert res == "success_payload"
    assert attempts == 3
    assert len(retries_recorded) == 2


def test_retry_with_backoff_exhausted():
    attempts = 0

    def persistent_failure():
        nonlocal attempts
        attempts += 1
        raise TimeoutError("Dead server")

    cfg = RetryConfig(max_retries=3, initial_delay=0.01, backoff_factor=1.5, jitter=False)
    with pytest.raises(TimeoutError) as exc_info:
        retry_with_backoff(
            persistent_failure,
            retry_config=cfg,
            retryable_exceptions=(TimeoutError,),
        )
    assert attempts == 3
    assert "Dead server" in str(exc_info.value)
