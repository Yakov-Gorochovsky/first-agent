"""
tests/conftest.py - Shared pytest fixtures and mocks for Neural Swarm test suite.
"""

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

import pytest

os.environ.setdefault("CHECKPOINT_HMAC_SECRET", "test_checkpoint_secret_key_fixed_hash_1234567890")

from core.auth import AuthManager
from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.llm import LLMResponse
from core.roles import RoleRegistry
from core.storage import SQLiteStorage


class ScriptedMockLLM:
    """Deterministic LLM test double returning a sequence of scripted LLMResponse objects."""

    def __init__(self, responses: Optional[List[LLMResponse]] = None) -> None:
        self.model = "mock-deterministic-v1"
        self._responses = list(responses or [])
        self._idx = 0
        self.calls: List[Dict[str, Any]] = []

    def add_response(self, resp: LLMResponse) -> None:
        self._responses.append(resp)

    def generate(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> LLMResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self._idx < len(self._responses):
            resp = self._responses[self._idx]
            self._idx += 1
            return resp
        return LLMResponse(content="Default mock response.", prompt_tokens=10, completion_tokens=5)


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Provide an isolated temporary directory for test artifacts."""
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


@pytest.fixture
def event_bus() -> EventBus:
    """Provide a fresh EventBus instance."""
    return EventBus()


@pytest.fixture
def temp_storage(temp_dir: Path) -> SQLiteStorage:
    """Provide an isolated SQLiteStorage backend."""
    db_path = temp_dir / "test_swarm.db"
    return SQLiteStorage(db_path=db_path)


@pytest.fixture
def budget_tracker(event_bus: EventBus) -> BudgetTracker:
    """Provide a default BudgetTracker wired to event_bus."""
    config = BudgetConfig(
        max_swarm_steps=10,
        max_steps_per_agent=4,
        max_total_tokens=2000,
        max_wall_seconds=30.0,
    )
    return BudgetTracker(config=config, bus=event_bus)


@pytest.fixture
def role_registry(temp_dir: Path) -> RoleRegistry:
    """Provide a fresh RoleRegistry initialized from temporary path."""
    roles_file = temp_dir / "roles.json"
    return RoleRegistry(roles_file=roles_file)


@pytest.fixture
def auth_manager(temp_dir: Path) -> AuthManager:
    """Provide a fresh AuthManager with known test keys."""
    keys_file = temp_dir / "keys.json"
    manager = AuthManager(keys_file=keys_file)
    return manager


@pytest.fixture
def mock_llm() -> ScriptedMockLLM:
    """Provide an empty ScriptedMockLLM instance."""
    return ScriptedMockLLM()
