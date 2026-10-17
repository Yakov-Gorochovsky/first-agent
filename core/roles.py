"""
core/roles.py - Declarative Role Engine, Capability Bundles & Dynamic Role Registry.

Makes creating new agent roles effortless in 3 ways:
1. Declarative JSON (`workspace/roles.json`) or 1-line Python `RoleSpec`
2. High-level Capability Bundles (`@web`, `@code`, `@files`, `@memory`, `@all`)
   so you don't need to list individual low-level tool names
3. On-the-fly AI recruiting via the `spawn_specialist` tool or Web UI modal
"""

from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional, Tuple
from tools import AVAILABLE_TOOLS, WORKSPACE_DIR

ROLES_FILE = WORKSPACE_DIR / "roles.json"

# High-level Capability Bundles map human-friendly tags to underlying atomic tools
CAPABILITY_BUNDLES: Dict[str, List[str]] = {
    "@web": ["web_search", "fetch_url", "get_weather", "get_current_time"],
    "@code": ["run_python", "calculator"],
    "@files": ["save_note", "read_note", "list_notes"],
    "@memory": ["remember_fact", "recall_memories"],
    "@all": list(AVAILABLE_TOOLS.keys()),
}


def resolve_capabilities(capabilities: List[str]) -> List[str]:
    """Expand capability tags (e.g. ['@web', '@code']) and tool names into a deduplicated tool list."""
    resolved: List[str] = []
    for item in capabilities:
        token = item.strip()
        if not token:
            continue
        if token in CAPABILITY_BUNDLES:
            for t_name in CAPABILITY_BUNDLES[token]:
                if t_name not in resolved:
                    resolved.append(t_name)
        elif f"@{token}" in CAPABILITY_BUNDLES:
            for t_name in CAPABILITY_BUNDLES[f"@{token}"]:
                if t_name not in resolved:
                    resolved.append(t_name)
        elif token in AVAILABLE_TOOLS and token not in resolved:
            resolved.append(token)
    return resolved


@dataclass
class RoleSpec:
    name: str
    title: str
    instructions: str
    capabilities: List[str] = field(default_factory=lambda: ["@web", "@code"])
    color: str = "#00ff9d"
    is_builtin: bool = False

    @property
    def tool_names(self) -> List[str]:
        return resolve_capabilities(self.capabilities)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["resolved_tools"] = self.tool_names
        return d


DEFAULT_ROLES: List[RoleSpec] = [
    RoleSpec(
        name="researcher",
        title="Web & Live Data Specialist",
        instructions=(
            "Gather live facts from the web, read URLs, check weather, or check current time. "
            "Synthesize concise, accurate findings."
        ),
        capabilities=["@web"],
        color="#00ff9d",
        is_builtin=True,
    ),
    RoleSpec(
        name="coder",
        title="Python & Math Engineer",
        instructions=(
            "Solve math problems, write & execute Python code in the sandbox, "
            "and read/write files in workspace/. Always verify your output."
        ),
        capabilities=["@code", "@files"],
        color="#ffb800",
        is_builtin=True,
    ),
    RoleSpec(
        name="critic",
        title="QA & Verification Reviewer",
        instructions=(
            "Review outputs for accuracy, completeness, and edge cases. "
            "Use calculator or read_note to double-check claims before approving."
        ),
        capabilities=["calculator", "@files"],
        color="#b56bff",
        is_builtin=True,
    ),
]


class RoleRegistry:
    """Manages persistent and dynamically spawned specialist roles."""

    def __init__(self, roles_file: Path = ROLES_FILE) -> None:
        self.roles_file = roles_file
        self.roles_file.parent.mkdir(parents=True, exist_ok=True)
        self._roles: Dict[str, RoleSpec] = {}
        self.load()

    def load(self) -> None:
        """Load roles from workspace/roles.json, seeding defaults if missing."""
        self._roles = {r.name: r for r in DEFAULT_ROLES}
        if self.roles_file.exists():
            try:
                raw_list = json.loads(self.roles_file.read_text(encoding="utf-8"))
                if isinstance(raw_list, list):
                    self._roles.clear()
                    for item in raw_list:
                        if isinstance(item, dict) and item.get("name"):
                            spec = RoleSpec(
                                name=self._clean_name(str(item["name"])),
                                title=str(item.get("title", item["name"])),
                                instructions=str(item.get("instructions", "")),
                                capabilities=list(item.get("capabilities", ["@web", "@code"])),
                                color=str(item.get("color", "#00f0ff")),
                                is_builtin=bool(item.get("is_builtin", False)),
                            )
                            self._roles[spec.name] = spec
            except Exception:
                pass
        if not self._roles:
            self._roles = {r.name: r for r in DEFAULT_ROLES}
        self.save()

    def save(self) -> None:
        payload = [asdict(r) for r in self._roles.values()]
        self.roles_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _clean_name(name: str) -> str:
        cleaned = re.sub(r"[^a-z0-9_]", "_", name.lower().strip()).strip("_")
        return cleaned or "specialist"

    def register_role(
        self,
        name: str,
        title: str,
        instructions: str,
        capabilities: Optional[List[str]] = None,
        color: str = "#00f0ff",
    ) -> RoleSpec:
        """Create or update a specialist role using Capability Bundles or tool names."""
        safe_name = self._clean_name(name)
        if safe_name in {"coordinator", "orchestrator", "user", "airlock"}:
            safe_name = f"{safe_name}_specialist"

        caps = capabilities if capabilities else ["@web", "@code"]
        spec = RoleSpec(
            name=safe_name,
            title=title.strip() or safe_name.title(),
            instructions=instructions.strip() or f"Act as {title} and solve assigned tasks accurately.",
            capabilities=caps,
            color=color or "#00f0ff",
            is_builtin=False,
        )
        self._roles[safe_name] = spec
        self.save()
        return spec

    def delete_role(self, name: str) -> bool:
        safe_name = self._clean_name(name)
        if safe_name in self._roles:
            del self._roles[safe_name]
            self.save()
            return True
        return False

    def get_role(self, name: str) -> Optional[RoleSpec]:
        return self._roles.get(self._clean_name(name))

    def list_roles(self) -> List[RoleSpec]:
        return list(self._roles.values())

    def list_dicts(self) -> List[Dict[str, Any]]:
        return [r.to_dict() for r in self._roles.values()]

    def make_spawn_specialist_tool(
        self, on_spawn: Optional[Callable[[RoleSpec], None]] = None
    ) -> Tuple[Callable[..., str], Dict[str, Any]]:
        """Build an AI-callable tool so Coordinator/Orchestrator can recruit new roles on the fly."""

        def spawn_specialist(
            name: str,
            title: str,
            instructions: str,
            capabilities: Optional[List[str]] = None,
        ) -> str:
            spec = self.register_role(
                name=name,
                title=title,
                instructions=instructions,
                capabilities=capabilities or ["@web", "@code"],
                color="#00f0ff",
            )
            if on_spawn:
                on_spawn(spec)
            return (
                f"Spawned specialist role '{spec.name}' ({spec.title}) with tools: {spec.tool_names}. "
                f"You can now delegate or send messages to '{spec.name}'."
            )

        schema = {
            "type": "function",
            "function": {
                "name": "spawn_specialist",
                "description": (
                    "Dynamically create and recruit a new specialist agent role when the current team "
                    "lacks a needed persona (e.g., 'security_auditor', 'data_scientist', 'copywriter')."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short snake_case identifier (e.g., 'sql_analyst').",
                        },
                        "title": {
                            "type": "string",
                            "description": "Human-readable role title (e.g., 'SQL & Data Analyst').",
                        },
                        "instructions": {
                            "type": "string",
                            "description": "System prompt instructions defining how this specialist works.",
                        },
                        "capabilities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of capability bundles: '@web', '@code', '@files', '@memory', or '@all'."
                            ),
                        },
                    },
                    "required": ["name", "title", "instructions"],
                },
            },
        }
        return spawn_specialist, schema
