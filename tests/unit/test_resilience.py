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


def test_extract_retry_delay_from_http_headers():
    from core.resilience import extract_retry_delay

    class DummyResponse:
        def __init__(self, headers):
            self.headers = headers
            self.status_code = 429

    class DummyHttpError(Exception):
        def __init__(self, headers):
            self.response = DummyResponse(headers)
            super().__init__("429 Client Error: Too Many Requests")

    # 1. Standard Retry-After seconds
    err1 = DummyHttpError({"retry-after": "12"})
    assert extract_retry_delay(err1) == 12.0

    # 2. retry-after-ms
    err2 = DummyHttpError({"retry-after-ms": "15000"})
    assert extract_retry_delay(err2) == 15.0

    # 3. x-ratelimit-reset-requests
    err3 = DummyHttpError({"x-ratelimit-reset-requests": "6s"})
    assert extract_retry_delay(err3) == 6.0

    # 4. x-ratelimit-reset-tokens with ms
    err4 = DummyHttpError({"x-ratelimit-reset-tokens": "2500ms"})
    assert extract_retry_delay(err4) == 2.5


def test_extract_retry_delay_from_error_bodies_and_messages():
    from core.resilience import extract_retry_delay

    class BodyError(Exception):
        def __init__(self, msg, body):
            self.body = body
            super().__init__(msg)

    # 1. JSON body with retry_after
    err_body = BodyError("rate_limit", {"error": {"retry_after": 8.5}})
    assert extract_retry_delay(err_body) == 8.5

    # 2. OpenAI-style message string
    err_openai = Exception("Rate limit reached for model gpt-4o. Please try again in 4.2s.")
    assert extract_retry_delay(err_openai) == 4.2

    # 3. Google Gemini Quota exceeded message
    err_gemini = Exception(
        "Generate content requests per minute limit exceeded on Free Tier. "
        "Please retry in 21.744843478s. status: RESOURCE_EXHAUSTED"
    )
    assert extract_retry_delay(err_gemini) == 21.744843478

    # 4. Milliseconds in message
    err_ms = Exception("Too Many Requests. try again in 800ms")
    assert extract_retry_delay(err_ms) == 0.8


def test_extract_retry_delay_progressive_fallback():
    from core.resilience import extract_retry_delay

    err_generic_429 = Exception("HTTP 429 Too Many Requests")
    assert extract_retry_delay(err_generic_429, attempt=1) == 5.0
    assert extract_retry_delay(err_generic_429, attempt=2) == 12.0
    assert extract_retry_delay(err_generic_429, attempt=3) == 25.0
    assert extract_retry_delay(err_generic_429, attempt=4) == 45.0

    # Non-rate-limit error should return None
    err_val = ValueError("Invalid parameter value 'foo'")
    assert extract_retry_delay(err_val, attempt=1) is None


def test_circuit_breaker_does_not_trip_on_rate_limits():
    cb = CircuitBreaker("cb_rate_limit", CircuitBreakerConfig(failure_threshold=2, recovery_timeout=1.0))

    class RateLimitEx(Exception):
        def __init__(self):
            self.status_code = 429
            super().__init__("429 Rate limit exceeded. retry in 10s")

    # Rate limit exceptions should NOT count against failure_threshold
    for _ in range(5):
        with pytest.raises(RateLimitEx):
            cb.call(lambda: (_ for _ in ()).throw(RateLimitEx()))

    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


def test_direct_retry_after_attribute():
    from core.resilience import extract_retry_delay

    class CustomRateLimit(Exception):
        retry_after = 18.5

    err = CustomRateLimit("rate limit active")
    assert extract_retry_delay(err) == 18.5


def test_worker_agent_empty_resource_retry_and_exhaustion(temp_storage, event_bus):
    """Verifies that empty resource retries are refunded from the step count and exhausted on attempt 3."""
    from core.budget import BudgetConfig, BudgetTracker
    from core.llm import LLMResponse, ToolCallRequest
    from core.swarm import WorkerAgent
    from tests.conftest import ScriptedMockLLM

    responses = [
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="list_notes", arguments={"tenant_id": "empty_tenant_test"})],
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c2", name="list_notes", arguments={"tenant_id": "empty_tenant_test"})],
        ),
        LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c3", name="list_notes", arguments={"tenant_id": "empty_tenant_test"})],
        ),
        LLMResponse(content="Exhausted check handled."),
    ]
    mock_llm = ScriptedMockLLM(responses)
    budget = BudgetTracker(config=BudgetConfig(max_swarm_steps=10, max_steps_per_agent=5))
    agent = WorkerAgent(
        name="critic",
        role="QA",
        instructions="Verify notes",
        llm=mock_llm,
        storage=temp_storage,
        budget=budget,
        bus=event_bus,
        tool_names=["list_notes"],
        max_steps=5,
    )
    res = agent.execute_task("Check notes", tenant_id="empty_tenant_test")
    assert res == "Exhausted check handled."
    # With 2 empty retries refunded, agent steps should be 2 instead of 4
    assert budget.agent_steps["critic"] == 2


