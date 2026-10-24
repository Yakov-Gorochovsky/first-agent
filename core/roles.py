"""
core/roles.py - Declarative Role Engine, Capability Bundles & Dynamic Role Registry.

100% data-driven:
- Seed roles live in `config/default_roles.json` (version-controlled, zero roles hardcoded in Python)
- Runtime customizations are persisted in `workspace/roles.json`
- High-level Capability Bundles (`@web`, `@code`, `@files`, `@memory`, `@all`)
- On-the-fly AI recruiting via the `spawn_specialist` tool or Web UI modal
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config import CONFIG
from tools import AVAILABLE_TOOLS

DEFAULT_ROLES_FILE = CONFIG.default_roles_path
ROLES_FILE = CONFIG.roles_path

# Loaded from config/settings.json + '@all' dynamic bundle
CAPABILITY_BUNDLES: Dict[str, List[str]] = {
    **CONFIG.capability_bundles,
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
    max_steps: Optional[int] = None

    @property
    def effective_max_steps(self) -> int:
        if self.max_steps is not None and self.max_steps > 0:
            return self.max_steps
        return CONFIG.max_steps_per_agent

    @property
    def tool_names(self) -> List[str]:
        return resolve_capabilities(self.capabilities)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["max_steps"] = self.effective_max_steps
        d["resolved_tools"] = self.tool_names
        return d


class RoleRegistry:
    """Manages persistent and dynamically spawned specialist roles from JSON config files."""

    def __init__(
        self,
        roles_file: Path = ROLES_FILE,
        default_roles_file: Path = DEFAULT_ROLES_FILE,
    ) -> None:
        self.roles_file = roles_file
        self.default_roles_file = default_roles_file
        self.roles_file.parent.mkdir(parents=True, exist_ok=True)
        self._roles: Dict[str, RoleSpec] = {}
        self.load()

    def _parse_role_list(self, file_path: Path) -> Dict[str, RoleSpec]:
        parsed: Dict[str, RoleSpec] = {}
        if not file_path.exists():
            return parsed
        try:
            raw_list = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(raw_list, list):
                for item in raw_list:
                    if isinstance(item, dict) and item.get("name"):
                        raw_max_steps = item.get("max_steps")
                        spec = RoleSpec(
                            name=self._clean_name(str(item["name"])),
                            title=str(item.get("title", item["name"])),
                            instructions=str(item.get("instructions", "")),
                            capabilities=list(item.get("capabilities", ["@web", "@code"])),
                            color=str(item.get("color", "#00f0ff")),
                            is_builtin=bool(item.get("is_builtin", False)),
                            max_steps=int(raw_max_steps) if raw_max_steps is not None else None,
                        )
                        parsed[spec.name] = spec
        except Exception:
            pass
        return parsed

    def load(self) -> None:
        """Load roles from workspace/roles.json, falling back to config/default_roles.json."""
        self._roles = self._parse_role_list(self.roles_file)
        if not self._roles:
            self._roles = self._parse_role_list(self.default_roles_file)
            self.save()

    def reset_to_defaults(self) -> List[RoleSpec]:
        """Restore roles from config/default_roles.json."""
        self._roles = self._parse_role_list(self.default_roles_file)
        self.save()
        return self.list_roles()

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
        max_steps: Optional[int] = None,
    ) -> RoleSpec:
        """Create or update a specialist role using Capability Bundles or tool names."""
        safe_name = self._clean_name(name)
        if safe_name in {"coordinator", "orchestrator", "user", "airlock"}:
            safe_name = f"{safe_name}_specialist"

        caps = capabilities if capabilities else ["@web", "@code"]
        decided_steps = (
            int(max_steps)
            if (max_steps is not None and int(max_steps) > 0)
            else CONFIG.max_steps_per_agent
        )
        spec = RoleSpec(
            name=safe_name,
            title=title.strip() or safe_name.title(),
            instructions=instructions.strip() or f"Act as {title} and solve assigned tasks accurately.",
            capabilities=caps,
            color=color or "#00f0ff",
            is_builtin=False,
            max_steps=decided_steps,
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
            max_steps: Optional[int] = None,
        ) -> str:
            base_caps = capabilities if capabilities else ["@web", "@code", "@files", "@memory"]
            merged_caps = list(dict.fromkeys([*base_caps, "@web", "@files", "@memory"]))
            spec = self.register_role(
                name=name,
                title=title,
                instructions=instructions,
                capabilities=merged_caps,
                color="#00f0ff",
                max_steps=max_steps,
            )
            if on_spawn:
                on_spawn(spec)
            return (
                f"Spawned specialist role '{spec.name}' ({spec.title}, max_steps={spec.effective_max_steps}) "
                f"with tools: {spec.tool_names}. You can now delegate or send messages to '{spec.name}'."
            )

        schema = {
            "type": "function",
            "function": {
                "name": "spawn_specialist",
                "description": (
                    "Dynamically create and recruit a new specialist agent role to test a specific hypothesis, "
                    "attack vector, or domain angle discovered during reconnaissance.\n"
                    "CRITICAL CAPABILITY RULES:\n"
                    "- '@web' (fetch_url, web_search): REQUIRED if the specialist needs to probe live URLs, HTTP headers, endpoints, or raw HTML/DOM (via fetch_url(url, include_raw=True)).\n"
                    "- '@code' (run_python, calculator): Runs in an OFFLINE sandbox (no internet access). Use for computation, parsing, or local scripts.\n"
                    "- '@files' & '@memory': Automatically included for workspace notes and shared memory."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Short snake_case identifier (e.g., 'cors_prober', 'dom_inspector', 'sql_analyst').",
                        },
                        "title": {
                            "type": "string",
                            "description": "Human-readable role title.",
                        },
                        "instructions": {
                            "type": "string",
                            "description": (
                                "Actionable operational briefing for the specialist: specify (1) the exact hypothesis or vector to test, "
                                "(2) which tools to actively run (do NOT tell it to merely read/summarize prior notes), and "
                                "(3) what empirical evidence to verify and save."
                            ),
                        },
                        "capabilities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "List of capability bundles: '@web' (required for any live URL/DOM/network probing), "
                                "'@code' (offline Python/math sandbox), '@files', '@memory', or '@all'."
                            ),
                        },
                        "max_steps": {
                            "type": "integer",
                            "description": (
                                "Maximum LLM steps allowed for this specialist per run (default 12-16)."
                            ),
                        },
                    },
                    "required": ["name", "title", "instructions"],
                },
            },
        }
        return spawn_specialist, schema
