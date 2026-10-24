"""
core/registry.py - FP-12: Capability, Tool, and Role Specification Registry.

Architectural Purpose:
Decouples core swarm orchestration from fixed personas and static toolsets.
Provides:
1. ToolMetadata & ToolRegistry:
   - Declarative metadata: risk_class ('low', 'medium', 'high'), side_effects (bool),
     data_classification ('public', 'internal', 'restricted'), and category.
   - Dynamic registration without modifying core orchestrator code.
2. RoleSpec:
   - Pure data model: name, purpose, instructions, capabilities, tool_list,
     model_policy, skill_tags, limits, and tenant scoping.
   - Conformance: Zero hardcoded persona names in core engines.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import tools
from core.config import CONFIG

logger = logging.getLogger("neural_swarm.registry")


@dataclass(frozen=True)
class ToolMetadata:
    """Declarative capability metadata declaring risk class, side effects, and sensitivity."""
    name: str
    fn: Callable[..., Any]
    schema: Dict[str, Any]
    risk_class: str = "medium"  # "low" | "medium" | "high"
    side_effects: bool = False
    data_classification: str = "internal"  # "public" | "internal" | "restricted"
    category: str = "general"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "risk_class": self.risk_class,
            "side_effects": self.side_effects,
            "data_classification": self.data_classification,
            "category": self.category,
            "schema": self.schema,
        }


class ToolRegistry:
    """Thread-safe registry for AI-callable tools with security and side-effect declarations."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolMetadata] = {}
        self._seed_builtins()

    def _seed_builtins(self) -> None:
        """Seed registry with system default tools and their security classifications."""
        schemas_by_name = {
            s["function"]["name"]: s
            for s in tools.TOOL_SCHEMAS
            if "function" in s and "name" in s["function"]
        }

        # Safe read/computation tools (low risk, no side effects)
        if "calculator" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="calculator",
                    fn=tools.calculator,
                    schema=schemas_by_name.get("calculator", {}),
                    risk_class="low",
                    side_effects=False,
                    data_classification="public",
                    category="compute",
                )
            )
        if "get_current_time" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="get_current_time",
                    fn=tools.get_current_time,
                    schema=schemas_by_name.get("get_current_time", {}),
                    risk_class="low",
                    side_effects=False,
                    data_classification="public",
                    category="compute",
                )
            )
        if "get_weather" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="get_weather",
                    fn=tools.get_weather,
                    schema=schemas_by_name.get("get_weather", {}),
                    risk_class="low",
                    side_effects=False,
                    data_classification="public",
                    category="web",
                )
            )
        if "read_note" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="read_note",
                    fn=tools.read_note,
                    schema=schemas_by_name.get("read_note", {}),
                    risk_class="medium",
                    side_effects=False,
                    data_classification="internal",
                    category="workspace",
                )
            )
        if "list_notes" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="list_notes",
                    fn=tools.list_notes,
                    schema=schemas_by_name.get("list_notes", {}),
                    risk_class="low",
                    side_effects=False,
                    data_classification="internal",
                    category="workspace",
                )
            )
        if "recall_memories" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="recall_memories",
                    fn=tools.recall_memories,
                    schema=schemas_by_name.get("recall_memories", {}),
                    risk_class="medium",
                    side_effects=False,
                    data_classification="internal",
                    category="memory",
                )
            )

        # Untrusted Web Ingestion (medium risk, no local side effects)
        if "web_search" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="web_search",
                    fn=tools.web_search,
                    schema=schemas_by_name.get("web_search", {}),
                    risk_class="medium",
                    side_effects=False,
                    data_classification="public",
                    category="web",
                )
            )
        if "fetch_url" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="fetch_url",
                    fn=tools.fetch_url,
                    schema=schemas_by_name.get("fetch_url", {}),
                    risk_class="medium",
                    side_effects=False,
                    data_classification="public",
                    category="web",
                )
            )

        # High-Risk / Side-Effecting Tools (code execution, file writing, persistent mutations)
        if "run_python" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="run_python",
                    fn=tools.run_python,
                    schema=schemas_by_name.get("run_python", {}),
                    risk_class="high",
                    side_effects=True,
                    data_classification="restricted",
                    category="code",
                )
            )
        if "save_note" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="save_note",
                    fn=tools.save_note,
                    schema=schemas_by_name.get("save_note", {}),
                    risk_class="high",
                    side_effects=True,
                    data_classification="internal",
                    category="workspace",
                )
            )
        if "remember_fact" in tools.AVAILABLE_TOOLS:
            self.register(
                ToolMetadata(
                    name="remember_fact",
                    fn=tools.remember_fact,
                    schema=schemas_by_name.get("remember_fact", {}),
                    risk_class="high",
                    side_effects=True,
                    data_classification="internal",
                    category="memory",
                )
            )

    def register(self, meta: ToolMetadata) -> None:
        """Register or override a tool specification."""
        self._tools[meta.name] = meta

    def unregister(self, name: str) -> bool:
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def get(self, name: str) -> Optional[ToolMetadata]:
        return self._tools.get(name)

    def list_tools(self) -> List[ToolMetadata]:
        return list(self._tools.values())

    def list_by_risk(self, risk_class: str) -> List[ToolMetadata]:
        return [t for t in self._tools.values() if t.risk_class == risk_class]

    def list_side_effecting(self) -> List[ToolMetadata]:
        return [t for t in self._tools.values() if t.side_effects]


@dataclass
class DynamicRoleSpec:
    """
    FP-12 Data Model: Role specification containing zero hardcoded runtime behavior.
    """
    name: str
    purpose: str
    instructions: str
    capabilities: List[str] = field(default_factory=list)
    tool_list: List[str] = field(default_factory=list)
    model_policy: Dict[str, Any] = field(default_factory=dict)
    skill_tags: List[str] = field(default_factory=list)
    limits: Dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "default"
    color: str = "#00f0ff"

    @property
    def max_steps(self) -> int:
        return int(self.limits.get("max_steps", CONFIG.max_steps_per_agent))

    def resolve_effective_tools(self, tool_registry: Optional[ToolRegistry] = None) -> List[str]:
        """Combine explicit tool_list and expanded capability bundles against registered tools."""
        active_reg = tool_registry or GLOBAL_TOOL_REGISTRY
        resolved: Set[str] = set(self.tool_list)

        for cap in self.capabilities:
            norm_cap = cap.lower().strip()
            if norm_cap in {"@all", "all"}:
                resolved.update([t.name for t in active_reg.list_tools()])
            elif norm_cap in {"@web", "web"}:
                resolved.update(["web_search", "fetch_url", "get_weather"])
            elif norm_cap in {"@code", "code"}:
                resolved.update(["run_python", "calculator"])
            elif norm_cap in {"@files", "files", "@workspace"}:
                resolved.update(["save_note", "read_note", "list_notes"])
            elif norm_cap in {"@memory", "memory"}:
                resolved.update(["remember_fact", "recall_memories"])
            elif active_reg.get(norm_cap):
                resolved.add(norm_cap)

        # Filter only existing registered tools
        return [name for name in resolved if active_reg.get(name) is not None]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "instructions": self.instructions,
            "capabilities": self.capabilities,
            "tool_list": self.tool_list,
            "model_policy": self.model_policy,
            "skill_tags": self.skill_tags,
            "limits": self.limits,
            "tenant_id": self.tenant_id,
            "color": self.color,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DynamicRoleSpec":
        return cls(
            name=str(data.get("name", "specialist")).strip().lower(),
            purpose=str(data.get("purpose", "")).strip(),
            instructions=str(data.get("instructions", "")).strip(),
            capabilities=list(data.get("capabilities", [])),
            tool_list=list(data.get("tool_list", [])),
            model_policy=dict(data.get("model_policy", {})),
            skill_tags=list(data.get("skill_tags", [])),
            limits=dict(data.get("limits", {})),
            tenant_id=str(data.get("tenant_id", "default")),
            color=str(data.get("color", "#00f0ff")),
        )


GLOBAL_TOOL_REGISTRY = ToolRegistry()
