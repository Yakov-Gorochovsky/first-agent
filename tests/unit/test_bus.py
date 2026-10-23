"""
tests/unit/test_bus.py - Unit tests for EventBus pub/sub, deque retention, and subscriber isolation.
"""

from collections import deque

import pytest

from core.bus import EventBus, SwarmEvent


@pytest.mark.unit
def test_event_bus_publish_subscribe(event_bus: EventBus) -> None:
    """Emitted events are received by subscribers and recorded in history."""
    received = []

    def subscriber(evt: SwarmEvent) -> None:
        received.append(evt)

    event_bus.subscribe(subscriber)

    event_bus.emit(
        event_type="task_start",
        agent_name="coordinator",
        tenant_id="client_a",
        task="Run tests",
    )

    assert len(received) == 1
    assert received[0].event_type == "task_start"
    assert received[0].tenant_id == "client_a"
    assert received[0].data["task"] == "Run tests"
    assert len(event_bus.history) == 1


@pytest.mark.unit
def test_event_bus_bounded_history_retention() -> None:
    """EventBus history must bound to maxlen (1000) to avoid memory leaks."""
    bus = EventBus(max_history=1000)
    assert isinstance(bus.history, deque)
    assert bus.history.maxlen == 1000

    for i in range(1050):
        bus.emit("tick", agent_name="agent", tenant_id="tenant", iteration=i)

    assert len(bus.history) == 1000
    assert bus.history[0].data["iteration"] == 50  # oldest 50 events pruned


@pytest.mark.unit
def test_event_bus_subscriber_exception_isolated(event_bus: EventBus) -> None:
    """An exception in one subscriber does not disrupt other subscribers or crash emit."""
    good_calls = []

    def bad_sub(evt: SwarmEvent) -> None:
        raise RuntimeError("Subscriber crashed!")

    def good_sub(evt: SwarmEvent) -> None:
        good_calls.append(evt.event_type)

    event_bus.subscribe(bad_sub)
    event_bus.subscribe(good_sub)

    event_bus.emit("test_event", agent_name="worker", tenant_id="tenant")

    assert len(good_calls) == 1
    assert good_calls[0] == "test_event"
