"""
core/bus.py - Decoupled Pub/Sub Event Bus for Swarm Observability.

Every component (Agents, Tools, Budgets, Orchestrator) emits structured SwarmEvents
to the EventBus. Terminal CLIs, loggers, and SSE Web UIs subscribe to the bus
without coupling the agent logic to any specific UI.
"""

from collections import deque
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Callable, Dict, List, Optional

_LOGGER = logging.getLogger("EventBus")


@dataclass
class SwarmEvent:
    event_type: str  # e.g., "agent_start", "tool_call", "tool_result", "delegation", "budget", "skill_learned", "agent_finish"
    agent_name: str
    tenant_id: str
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "agent_name": self.agent_name,
            "tenant_id": self.tenant_id,
            "data": self.data,
            "timestamp": self.timestamp,
        }


EventHandler = Callable[[SwarmEvent], None]


class EventBus:
    """Synchronous/Async-ready in-process pub/sub event bus with bounded memory retention."""

    def __init__(self, max_history: int = 1000) -> None:
        self._subscribers: List[EventHandler] = []
        self.history: deque[SwarmEvent] = deque(maxlen=max_history)

    def subscribe(self, handler: EventHandler) -> None:
        """Register a listener function that receives every SwarmEvent."""
        if handler not in self._subscribers:
            self._subscribers.append(handler)

    def emit(
        self,
        event_type: str,
        agent_name: str,
        tenant_id: str = "default",
        **data: Any,
    ) -> SwarmEvent:
        """Publish an event to all subscribers."""
        event = SwarmEvent(
            event_type=event_type,
            agent_name=agent_name,
            tenant_id=tenant_id,
            data=data,
        )
        self.history.append(event)
        for handler in self._subscribers:
            try:
                handler(event)
            except Exception as err:
                _LOGGER.warning(
                    "Subscriber %s failed processing event '%s': %s",
                    getattr(handler, "__name__", str(handler)),
                    event_type,
                    err,
                )
        return event
