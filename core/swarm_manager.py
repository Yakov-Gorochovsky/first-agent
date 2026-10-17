"""
core/swarm_manager.py - Multi-Swarm Deployment & Cross-Swarm Communication Engine.

Allows deploying N independent Swarm instances (e.g., `victim`, `attacker`, `auditor`, `client_a`)
from declarative config (`config/default_swarms.json` -> `workspace/swarms.json`) or live via the
Web UI / CLI without ever creating a custom board per experiment.

Each deployed Swarm has:
- Its own `swarm_id` (which maps to its isolated SQLite `tenant_id` vault)
- Its own `topology` (`peer` or `orchestrator`)
- Its own `model` (defaults to `CONFIG.model_name` or any custom Ollama/API model)
- Its own assigned `roles` from `RoleRegistry`
- Its own `directive` and `allow_cross_swarm` bridge (`send_external_swarm_message`)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Optional

from core.budget import BudgetConfig, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider, OpenAICompatibleProvider
from core.peer_swarm import PeerSwarm
from core.roles import RoleRegistry
from core.storage import StorageBackend, sanitize_tenant_id
from core.swarm import OrchestratorSwarm

DEFAULT_SWARMS_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "default_swarms.json"
MAX_CROSS_SWARM_DEPTH = 3


@dataclass
class SwarmDeploymentSpec:
    swarm_id: str
    title: str
    topology: str = "peer"
    model: str = ""
    color: str = "#38bdf8"
    roles: List[str] = field(default_factory=lambda: ["researcher", "coder", "critic"])
    allow_cross_swarm: bool = False
    directive: str = ""
    seed_memories: Dict[str, str] = field(default_factory=dict)

    def to_dict(self, role_registry: Optional[RoleRegistry] = None) -> Dict[str, Any]:
        resolved_roles = []
        if role_registry:
            all_map = {r.name: r.to_dict() for r in role_registry.list_roles()}
            for r_name in self.roles:
                if r_name in all_map:
                    resolved_roles.append(all_map[r_name])
        return {
            "swarm_id": self.swarm_id,
            "title": self.title,
            "topology": self.topology,
            "model": self.model or CONFIG.model_name,
            "color": self.color,
            "roles": self.roles,
            "resolved_roles": resolved_roles,
            "allow_cross_swarm": self.allow_cross_swarm,
            "directive": self.directive,
            "seed_memories": self.seed_memories,
        }


class SwarmManager:
    """Manages deployed Swarm instances and routes cross-swarm messages on a unified board."""

    def __init__(
        self,
        storage: StorageBackend,
        bus: EventBus,
        role_registry: RoleRegistry,
        default_llm: Optional[LLMProvider] = None,
        swarms_file: Optional[Path] = None,
    ) -> None:
        self.storage = storage
        self.bus = bus
        self.role_registry = role_registry
        self.default_llm = default_llm or OpenAICompatibleProvider()
        self.swarms_file = swarms_file or (CONFIG.workspace_dir / "swarms.json")
        self._deployments: Dict[str, SwarmDeploymentSpec] = {}
        self._last_budgets: Dict[str, BudgetTracker] = {}
        self._load()

    def _load_default_seed(self) -> List[Dict[str, Any]]:
        if DEFAULT_SWARMS_CONFIG_PATH.exists():
            try:
                data = json.loads(DEFAULT_SWARMS_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except Exception:
                pass
        return [
            {
                "swarm_id": "default_swarm",
                "title": "Primary Swarm",
                "topology": "peer",
                "model": "",
                "color": "#38bdf8",
                "roles": ["researcher", "coder", "critic", "security_reviewer"],
                "allow_cross_swarm": True,
                "directive": "",
                "seed_memories": {},
            }
        ]

    def _load(self) -> None:
        if not self.swarms_file.exists():
            seed_items = self._load_default_seed()
            for item in seed_items:
                self._register_from_dict(item, save=False)
            self._save()
            return

        try:
            raw = json.loads(self.swarms_file.read_text(encoding="utf-8"))
            if isinstance(raw, list) and raw:
                self._deployments.clear()
                for item in raw:
                    self._register_from_dict(item, save=False)
                return
        except Exception:
            pass

        for item in self._load_default_seed():
            self._register_from_dict(item, save=False)
        self._save()

    def _register_from_dict(self, item: Dict[str, Any], save: bool = True) -> SwarmDeploymentSpec:
        s_id = sanitize_tenant_id(str(item.get("swarm_id", "swarm_1")).lower())
        spec = SwarmDeploymentSpec(
            swarm_id=s_id,
            title=str(item.get("title", s_id)),
            topology=str(item.get("topology", "peer")).lower() if str(item.get("topology", "peer")).lower() in {"peer", "orchestrator"} else "peer",
            model=str(item.get("model", "")).strip(),
            color=str(item.get("color", "#38bdf8")),
            roles=list(item.get("roles", ["researcher", "coder", "critic"])),
            allow_cross_swarm=bool(item.get("allow_cross_swarm", False)),
            directive=str(item.get("directive", "")),
            seed_memories=dict(item.get("seed_memories", {})),
        )
        self._deployments[s_id] = spec
        # Seed initial memories into the swarm's isolated SQLite tenant partition if not already set
        existing_mem = self.storage.get_tenant_memories(s_id)
        for k, v in spec.seed_memories.items():
            if k not in existing_mem:
                self.storage.save_tenant_memory(s_id, k, v)
        if save:
            self._save()
        return spec

    def _save(self) -> None:
        self.swarms_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "swarm_id": s.swarm_id,
                "title": s.title,
                "topology": s.topology,
                "model": s.model,
                "color": s.color,
                "roles": s.roles,
                "allow_cross_swarm": s.allow_cross_swarm,
                "directive": s.directive,
                "seed_memories": s.seed_memories,
            }
            for s in self._deployments.values()
        ]
        self.swarms_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_swarms(self) -> List[SwarmDeploymentSpec]:
        return list(self._deployments.values())

    def list_dicts(self) -> List[Dict[str, Any]]:
        return [s.to_dict(self.role_registry) for s in self._deployments.values()]

    def get_swarm(self, swarm_id: str) -> Optional[SwarmDeploymentSpec]:
        return self._deployments.get(sanitize_tenant_id(swarm_id.lower()))

    def deploy_swarm(
        self,
        swarm_id: str,
        title: str,
        topology: str = "peer",
        model: str = "",
        color: str = "#38bdf8",
        roles: Optional[List[str]] = None,
        allow_cross_swarm: bool = False,
        directive: str = "",
        seed_memories: Optional[Dict[str, str]] = None,
    ) -> SwarmDeploymentSpec:
        spec = self._register_from_dict(
            {
                "swarm_id": swarm_id,
                "title": title,
                "topology": topology,
                "model": model,
                "color": color,
                "roles": roles or [r.name for r in self.role_registry.list_roles()],
                "allow_cross_swarm": allow_cross_swarm,
                "directive": directive,
                "seed_memories": seed_memories or {},
            },
            save=True,
        )
        self.bus.emit(
            "swarm_deployed",
            agent_name="system",
            tenant_id=spec.swarm_id,
            swarm=spec.to_dict(self.role_registry),
        )
        return spec

    def delete_swarm(self, swarm_id: str) -> bool:
        key = sanitize_tenant_id(swarm_id.lower())
        if key in self._deployments and len(self._deployments) > 1:
            del self._deployments[key]
            self._save()
            self.bus.emit("swarm_deleted", agent_name="system", tenant_id=key, swarm_id=key)
            return True
        return False

    def _redact_cross_swarm_egress(self, target_swarm_id: str, text: str) -> str:
        """
        Deterministic Cross-Swarm Egress DLP Guardrail:
        Scrubs any confidential values, canary tokens, or internal URLs stored in
        `target_swarm_id`'s isolated tenant vault before the reply crosses the bridge.
        """
        if not text:
            return text
        redacted = text
        vault = dict(self.storage.get_tenant_memories(target_swarm_id))
        spec = self._deployments.get(target_swarm_id)
        if spec and spec.seed_memories:
            for k, v in spec.seed_memories.items():
                vault.setdefault(k, v)

        sensitive_tokens: List[str] = []
        for val in vault.values():
            val_str = str(val).strip()
            if len(val_str) >= 6:
                sensitive_tokens.append(val_str)
            # Extract embedded URLs or structured canary tokens (e.g. CANARY-VAULT-7749-OMEGA)
            for match in re.findall(r"https?://[^\s'\"`)>]+|[A-Z0-9]{3,}(?:-[A-Z0-9]{2,}){2,}", val_str):
                if len(match) >= 6:
                    sensitive_tokens.append(match)

        for token in sorted(set(sensitive_tokens), key=len, reverse=True):
            if token in redacted:
                redacted = redacted.replace(token, "[REDACTED_VAULT_SECRET]")
        return redacted

    def _make_cross_swarm_tool(
        self, source_swarm_id: str, depth: int
    ) -> tuple[Dict[str, Callable[..., str]], List[Dict[str, Any]]]:
        other_swarms = [s_id for s_id in self._deployments if s_id != source_swarm_id]
        if not other_swarms or depth >= MAX_CROSS_SWARM_DEPTH:
            return {}, []

        def send_external_swarm_message(target_swarm_id: str, message: str) -> str:
            target = sanitize_tenant_id(target_swarm_id.lower().strip())
            if target not in self._deployments:
                return f"Error: Unknown target swarm '{target_swarm_id}'. Deployed swarms: {other_swarms}"
            if target == source_swarm_id:
                return "Error: Use send_peer_message for agents inside your own swarm."

            self.bus.emit(
                "cross_swarm_message",
                agent_name="coordinator",
                tenant_id=source_swarm_id,
                from_swarm=source_swarm_id,
                to_swarm=target,
                message=message,
            )

            raw_reply = self.run_swarm(target, message, _depth=depth + 1)
            reply = self._redact_cross_swarm_egress(target, raw_reply)

            self.bus.emit(
                "cross_swarm_reply",
                agent_name="coordinator",
                tenant_id=target,
                from_swarm=target,
                to_swarm=source_swarm_id,
                reply=reply,
            )
            return f"[Response from external swarm '{target}']:\n{reply}"

        schema = {
            "type": "function",
            "function": {
                "name": "send_external_swarm_message",
                "description": (
                    f"Send a prompt or message to another deployed external Swarm instance and wait for its response. "
                    f"Available external swarms: {other_swarms}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_swarm_id": {
                            "type": "string",
                            "description": f"Target swarm ID (one of: {', '.join(other_swarms)}).",
                        },
                        "message": {
                            "type": "string",
                            "description": "The prompt, query, or test payload to send to the target swarm.",
                        },
                    },
                    "required": ["target_swarm_id", "message"],
                },
            },
        }
        return {"send_external_swarm_message": send_external_swarm_message}, [schema]

    def run_swarm(
        self,
        swarm_id: str,
        prompt: str,
        topology_override: Optional[str] = None,
        _depth: int = 0,
    ) -> str:
        """Execute a deployed swarm instance (and any cross-swarm calls it triggers)."""
        key = sanitize_tenant_id(swarm_id.lower())
        spec = self._deployments.get(key)
        if spec is None:
            # Auto-deploy if user specifies a new tenant/swarm ID on the fly
            spec = self.deploy_swarm(swarm_id=key, title=f"Swarm ({key})", topology=topology_override or "peer")

        topo = (topology_override or spec.topology).lower()
        llm = (
            OpenAICompatibleProvider(model=spec.model)
            if spec.model and spec.model != CONFIG.model_name
            else self.default_llm
        )
        budget = BudgetTracker(config=BudgetConfig(), bus=self.bus, tenant_id=spec.swarm_id)
        self._last_budgets[spec.swarm_id] = budget

        extra_tools, extra_schemas = (
            self._make_cross_swarm_tool(spec.swarm_id, _depth)
            if spec.allow_cross_swarm
            else ({}, [])
        )

        if topo == "orchestrator":
            engine = OrchestratorSwarm(
                llm=llm,
                storage=self.storage,
                budget=budget,
                bus=self.bus,
                role_registry=self.role_registry,
                allowed_roles=spec.roles,
                directive=spec.directive,
                extra_tools=extra_tools,
                extra_schemas=extra_schemas,
            )
            return engine.run(prompt, tenant_id=spec.swarm_id)

        engine_peer = PeerSwarm(
            llm=llm,
            storage=self.storage,
            budget=budget,
            bus=self.bus,
            role_registry=self.role_registry,
            allowed_roles=spec.roles,
            directive=spec.directive,
            extra_tools=extra_tools,
            extra_schemas=extra_schemas,
        )
        return engine_peer.run(prompt, tenant_id=spec.swarm_id)
