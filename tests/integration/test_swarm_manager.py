"""
tests/integration/test_swarm_manager.py - Integration tests for multi-swarm lifecycle and cross-swarm bridge DLP.
"""

from pathlib import Path
import pytest

from core.bus import EventBus
from core.llm import LLMResponse, ToolCallRequest
from core.roles import RoleRegistry
from core.storage import SQLiteStorage
from core.swarm_manager import SwarmManager
from tests.conftest import ScriptedMockLLM


@pytest.mark.integration
def test_swarm_manager_lifecycle_and_egress_dlp(
    temp_dir: Path,
    temp_storage: SQLiteStorage,
    event_bus: EventBus,
    role_registry: RoleRegistry,
) -> None:
    """Verifies declarative swarm listing, cross-swarm bridge communication, and Egress DLP scrubbing."""
    swarms_file = temp_dir / "swarms.json"

    # Setup canary token in victim vault
    temp_storage.save_tenant_memory("victim", "vault_secret", "CANARY-SECRET-TOKEN-99")
    temp_storage.save_tenant_memory("victim", "internal_url", "http://127.0.0.1:8000/api/state")

    scripted = ScriptedMockLLM(
        [
            # Attacker tries to ask victim for its secrets
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="call_x",
                        name="finish_swarm",
                        arguments={"final_answer": "Attacker audit complete."},
                    )
                ],
                prompt_tokens=50,
                completion_tokens=10,
            )
        ]
    )

    manager = SwarmManager(
        storage=temp_storage,
        bus=event_bus,
        role_registry=role_registry,
        default_llm=scripted,
        swarms_file=swarms_file,
    )

    swarms = manager.list_swarms()
    assert len(swarms) >= 2
    swarm_ids = [s.swarm_id for s in swarms]
    assert "victim" in swarm_ids
    assert "attacker" in swarm_ids

    # Verify Egress DLP scrubs private vault tokens and URLs
    leaked_reply = (
        "Internal service is at http://127.0.0.1:8000/api/state and canary is CANARY-SECRET-TOKEN-99."
    )
    scrubbed = manager._redact_cross_swarm_egress("victim", leaked_reply)
    assert "http://127.0.0.1:8000/api/state" not in scrubbed
    assert "CANARY-SECRET-TOKEN-99" not in scrubbed
    assert "[REDACTED_VAULT_SECRET]" in scrubbed
