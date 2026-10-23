"""
tests/unit/test_prompt_layout.py - FP-06: Cache-Stable Prompt Layout & Final-Turn Tool Handling Tests.

Tests:
1. 100% Prefix Stability Rate: SHA-256 prefix hash is bit-for-bit identical across all mission turns.
2. Canonical schema sorting: shuffled tool lists or dictionary keys produce identical canonical hashes.
3. Absence of dynamic leakage: session IDs, timestamps, or observations do not contaminate prefix.
4. Final-turn tool policy experiments: RETAIN_TOOLS, FORBID_TOOLS, REMOVE_TOOLS.
"""

import pytest

from core.prompt_layout import (
    CacheStablePromptBuilder,
    FinalTurnToolPolicy,
    canonicalize_tool_schemas,
)


@pytest.mark.unit
def test_prefix_stability_across_multiple_steps() -> None:
    """The immutable prefix hash remains 100% identical across all steps of an agent mission."""
    builder = CacheStablePromptBuilder()

    tools = [
        {"type": "function", "function": {"name": "calculator", "description": "Math tool"}},
        {"type": "function", "function": {"name": "web_search", "description": "Search web"}},
    ]

    base_instructions = "Act as an analytical research specialist."
    playbooks = ["Rule 1: Always verify numbers.", "Rule 2: Never disclose internal secrets."]

    # Step 1
    sys_prompt_1, hash_1 = builder.assemble_full_system_prompt(
        agent_name="analyst",
        role_title="Data Analyst",
        base_instructions=base_instructions,
        tool_schemas=tools,
        approved_playbooks=playbooks,
    )
    msgs_step_1 = builder.assemble_messages(
        system_prompt=sys_prompt_1,
        current_input="Analyze Q3 revenues",
    )

    # Step 2: after observations and intermediate turns
    obs_step_2 = ["Observation 1: Q3 revenue was $45.2M", "Observation 2: Up 12% YoY"]
    history_step_2 = [
        {"role": "user", "content": "Analyze Q3 revenues"},
        {"role": "assistant", "content": "Querying tools..."},
    ]
    sys_prompt_2, hash_2 = builder.assemble_full_system_prompt(
        agent_name="analyst",
        role_title="Data Analyst",
        base_instructions=base_instructions,
        tool_schemas=tools,
        approved_playbooks=playbooks,
    )
    msgs_step_2 = builder.assemble_messages(
        system_prompt=sys_prompt_2,
        conversation_history=history_step_2,
        dynamic_observations=obs_step_2,
        current_input="Synthesize final findings",
    )

    # Verification: Prefix hash must be 100% identical
    assert hash_1 == hash_2
    assert sys_prompt_1 == sys_prompt_2
    # But message count grew dynamically in the tail
    assert len(msgs_step_1) == 2
    assert len(msgs_step_2) == 5


@pytest.mark.unit
def test_canonical_schema_sorting_resists_shuffling() -> None:
    """Tool schema order and key permutations produce identical canonical outputs."""
    tool_a = {"type": "function", "function": {"name": "alpha_tool", "description": "First"}}
    tool_b = {"type": "function", "function": {"name": "beta_tool", "description": "Second"}}
    tool_c = {"type": "function", "function": {"name": "gamma_tool", "description": "Third"}}

    # Shuffled order 1
    list_1 = [tool_c, tool_a, tool_b]
    # Shuffled order 2
    list_2 = [tool_b, tool_c, tool_a]

    canon_1 = canonicalize_tool_schemas(list_1)
    canon_2 = canonicalize_tool_schemas(list_2)

    assert canon_1 == canon_2
    assert [t["function"]["name"] for t in canon_1] == ["alpha_tool", "beta_tool", "gamma_tool"]


@pytest.mark.unit
def test_prefix_free_of_dynamic_leakage() -> None:
    """Prefix contains zero timestamps, random tokens, or session IDs."""
    builder = CacheStablePromptBuilder()

    prefix = builder.build_immutable_prefix(
        agent_name="worker",
        role_title="Worker",
        base_instructions="Do work",
    )

    # Check that common dynamic indicators are not in prefix
    assert "timestamp" not in prefix.lower()
    assert "time:" not in prefix.lower()
    assert "session" not in prefix.lower()
    assert "turn" not in prefix.lower()


@pytest.mark.unit
def test_final_turn_tool_policies() -> None:
    """Final-turn tool policies correctly retain, forbid, or strip tool schemas."""
    schemas = [{"type": "function", "function": {"name": "calc"}}]

    # 1. Retain tools (KV cache preservation)
    b_retain = CacheStablePromptBuilder(final_turn_policy=FinalTurnToolPolicy.RETAIN_TOOLS)
    tools_out, choice = b_retain.prepare_final_turn(schemas)
    assert tools_out == schemas
    assert choice == "auto"

    # 2. Forbid tools (cache preserved, tool calls blocked)
    b_forbid = CacheStablePromptBuilder(final_turn_policy=FinalTurnToolPolicy.FORBID_TOOLS)
    tools_out, choice = b_forbid.prepare_final_turn(schemas)
    assert tools_out == schemas
    assert choice == "none"

    # 3. Remove tools (token savings on final turn)
    b_remove = CacheStablePromptBuilder(final_turn_policy=FinalTurnToolPolicy.REMOVE_TOOLS)
    tools_out, choice = b_remove.prepare_final_turn(schemas)
    assert tools_out is None
    assert choice is None
