"""
core/tool_binding.py - Binds an agent's granted tools to its tenant.

Tools take a tenant from the closure built here, never from model arguments, so an agent can't reach
another tenant's notes or memory by naming it in a tool call.
"""

from typing import Any, Callable, Dict, List, Sequence, Tuple

from core.bus import EventBus
from core.storage import StorageBackend
from tools import AVAILABLE_TOOLS, TOOL_SCHEMAS

ToolMap = Dict[str, Callable[..., str]]

SHARE_SKILL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "share_global_skill",
        "description": (
            "Propose a reusable, generic lesson for the shared skills playbook. "
            "It is scrubbed of private data and reaches other agents only after an administrator approves it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Topic of the lesson."},
                "problem": {"type": "string", "description": "Generic description of the problem or pitfall."},
                "solution": {"type": "string", "description": "Generic reusable solution or practice."},
            },
            "required": ["category", "problem", "solution"],
        },
    },
}


def _memory_tools(storage: StorageBackend, tenant_id: str) -> ToolMap:
    def remember_fact(key: str, fact: str) -> str:
        storage.save_tenant_memory(tenant_id, key, fact)
        return f"Saved to tenant '{tenant_id}' private memory: {key} = {fact}"

    def recall_memories() -> str:
        memories = storage.get_tenant_memories(tenant_id)
        if not memories:
            return f"No private memories stored for tenant '{tenant_id}'."
        return "\n".join(f"- {key}: {value}" for key, value in memories.items())

    return {"remember_fact": remember_fact, "recall_memories": recall_memories}


def _note_tools(tenant_id: str) -> ToolMap:
    save, read, list_all = AVAILABLE_TOOLS["save_note"], AVAILABLE_TOOLS["read_note"], AVAILABLE_TOOLS["list_notes"]

    def save_note(filename: str, content: str, **_: Any) -> str:
        return save(filename, content, tenant_id=tenant_id)

    def read_note(filename: str, **_: Any) -> str:
        return read(filename, tenant_id=tenant_id)

    def list_notes(**_: Any) -> str:
        return list_all(tenant_id=tenant_id)

    return {"save_note": save_note, "read_note": read_note, "list_notes": list_notes}


def _share_skill_tool(storage: StorageBackend, bus: EventBus, agent_name: str, tenant_id: str) -> Callable[..., str]:
    def share_global_skill(category: str, problem: str, solution: str) -> str:
        sanitized = storage.add_global_skill(category, problem, solution)
        bus.emit("skill_learned", agent_name=agent_name, tenant_id=tenant_id, **sanitized)
        return (
            f"Submitted for review: [{sanitized['category']}] "
            f"{sanitized['problem_pattern']} -> {sanitized['solution_rule']}"
        )

    return share_global_skill


def bind_tools(
    tool_names: Sequence[str],
    storage: StorageBackend,
    bus: EventBus,
    agent_name: str,
    tenant_id: str,
    extra_tools: ToolMap,
    extra_schemas: List[Dict[str, Any]],
) -> Tuple[ToolMap, List[Dict[str, Any]]]:
    """Return the callables and schemas for exactly the tools this agent was granted."""
    granted = set(tool_names)
    tenant_scoped = {**_memory_tools(storage, tenant_id), **_note_tools(tenant_id)}
    tools_map = {
        name: tenant_scoped.get(name, AVAILABLE_TOOLS[name]) for name in tool_names if name in AVAILABLE_TOOLS
    }
    schemas = [schema for schema in TOOL_SCHEMAS if schema["function"]["name"] in granted]
    if "share_global_skill" in granted:
        tools_map["share_global_skill"] = _share_skill_tool(storage, bus, agent_name, tenant_id)
        schemas.append(SHARE_SKILL_SCHEMA)
    return {**tools_map, **extra_tools}, [*schemas, *extra_schemas]
