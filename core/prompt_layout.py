"""
core/prompt_layout.py - FP-06: Cache-Stable Three-Tier Prompt Serialization & Prefix Optimization.

Architectural Purpose:
Maximizes LLM prompt cache hit ratios (e.g. Anthropic prompt caching, OpenAI prompt caching,
vLLM prefix caching, Ollama KV buffer reuse) by partitioning prompts into three deterministic tiers:
1. Immutable Prefix: System persona, core policies, and canonical, deterministically sorted tool schemas.
   - GUARANTEE: Zero timestamps, zero random UUIDs, zero session counters in the prefix.
   - Prefix hash is 100% bit-for-bit stable across all mission turns.
2. Semi-Static Body: Approved domain playbooks and stable role guidelines.
3. Dynamic Tail: Ephemeral conversation history, tool observations, inbox messages, and task input.

Also implements configurable final-turn tool handling policies (RETAIN_TOOLS, FORBID_TOOLS, REMOVE_TOOLS).
"""

from enum import Enum
import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("neural_swarm.prompt_layout")


class FinalTurnToolPolicy(str, Enum):
    """Policy for handling tool schemas on the final synthesis turn."""
    RETAIN_TOOLS = "retain_tools"    # Keep tools attached; preserves KV prefix cache
    FORBID_TOOLS = "forbid_tools"    # Keep tools attached with tool_choice='none'
    REMOVE_TOOLS = "remove_tools"    # Strip tool schemas to save token count (breaks prefix cache)


def canonicalize_tool_schemas(schemas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Sort tool schemas deterministically by function name and canonicalize keys.
    Prevents cache eviction caused by dictionary key reordering or list order shuffling.
    """
    def _extract_name(s: Dict[str, Any]) -> str:
        fn = s.get("function", {})
        return str(fn.get("name", "")).strip().lower()

    sorted_schemas = sorted(schemas, key=_extract_name)
    canonical: List[Dict[str, Any]] = []
    for s in sorted_schemas:
        # Roundtrip through json with sorted keys for canonical representation
        dumped = json.dumps(s, sort_keys=True)
        canonical.append(json.loads(dumped))
    return canonical


class CacheStablePromptBuilder:
    """
    Builds LLM message structures adhering strictly to the Three-Tier Cache-Stable layout.
    """

    def __init__(
        self,
        final_turn_policy: FinalTurnToolPolicy = FinalTurnToolPolicy.RETAIN_TOOLS,
    ) -> None:
        self.final_turn_policy = final_turn_policy

    @staticmethod
    def build_immutable_prefix(
        agent_name: str,
        role_title: str,
        base_instructions: str,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """
        Tier 1: Immutable Prefix.
        Must be 100% deterministic and free of dynamic timestamps, run IDs, or memory.
        """
        clean_name = agent_name.strip().lower()
        clean_title = role_title.strip()
        clean_inst = base_instructions.strip()

        lines = [
            f"=== ROLE IDENTIFIER ===",
            f"Agent: {clean_name}",
            f"Title: {clean_title}",
            "",
            f"=== SYSTEM DIRECTIVE ===",
            clean_inst,
        ]

        if tool_schemas:
            canon_schemas = canonicalize_tool_schemas(tool_schemas)
            lines.extend([
                "",
                f"=== DECLARED CAPABILITIES ===",
                json.dumps(canon_schemas, indent=2, sort_keys=True),
            ])

        return "\n".join(lines).strip()

    @staticmethod
    def compute_prefix_hash(immutable_prefix: str) -> str:
        """Calculate SHA-256 hash of the immutable prefix to verify 100% stability across steps."""
        return hashlib.sha256(immutable_prefix.encode("utf-8")).hexdigest()

    @staticmethod
    def build_semi_static_body(approved_playbooks: Optional[List[str]] = None) -> str:
        """
        Tier 2: Semi-Static Body.
        Contains verified domain rules and role guidelines that change only across deployments.
        """
        if not approved_playbooks:
            return ""

        sorted_books = sorted(p.strip() for p in approved_playbooks if p.strip())
        lines = [
            "=== APPROVED DOMAIN PLAYBOOKS ===",
            *sorted_books,
        ]
        return "\n".join(lines).strip()

    def assemble_full_system_prompt(
        self,
        agent_name: str,
        role_title: str,
        base_instructions: str,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        approved_playbooks: Optional[List[str]] = None,
    ) -> Tuple[str, str]:
        """
        Assemble combined system prompt and return (system_prompt, prefix_hash).
        """
        prefix = self.build_immutable_prefix(agent_name, role_title, base_instructions, tool_schemas)
        prefix_hash = self.compute_prefix_hash(prefix)
        body = self.build_semi_static_body(approved_playbooks)

        if body:
            full_prompt = f"{prefix}\n\n{body}"
        else:
            full_prompt = prefix

        return full_prompt, prefix_hash

    def assemble_messages(
        self,
        system_prompt: str,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        dynamic_observations: Optional[List[str]] = None,
        current_input: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Tier 3: Dynamic Tail.
        Assembles full message list with dynamic observations and user task placed strictly at the tail.
        """
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

        if conversation_history:
            for msg in conversation_history:
                messages.append({
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                })

        # Append dynamic observations (if any)
        if dynamic_observations:
            obs_block = "\n\n".join(dynamic_observations)
            messages.append({
                "role": "user",
                "content": f"[OBSERVATIONS]\n{obs_block}",
            })

        if current_input:
            messages.append({
                "role": "user",
                "content": current_input,
            })

        return messages

    def prepare_final_turn(
        self,
        active_schemas: List[Dict[str, Any]],
    ) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
        """
        Determine tool schemas and tool_choice for the final synthesis turn based on policy.
        Returns: (schemas_to_pass, tool_choice_value)
        """
        if self.final_turn_policy == FinalTurnToolPolicy.RETAIN_TOOLS:
            # Full cache preservation
            return active_schemas, "auto"
        elif self.final_turn_policy == FinalTurnToolPolicy.FORBID_TOOLS:
            # Schemas retained to preserve prefix, but calls forbidden
            return active_schemas, "none"
        elif self.final_turn_policy == FinalTurnToolPolicy.REMOVE_TOOLS:
            # Schemas stripped to save tokens (breaks prefix cache)
            return None, None
        return active_schemas, "auto"
