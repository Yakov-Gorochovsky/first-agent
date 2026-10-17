"""
core/swarm.py - Multi-Agent Orchestrator & Worker Harness.

Connects decoupled interfaces:
- LLMProvider (Ollama, Hugging Face, Groq, Gemini, OpenAI)
- StorageBackend (SQLite multi-tenant private memories + PrivacyAirlock global skills)
- BudgetTracker (step, token, and wall-time caps)
- EventBus (real-time telemetry for CLI and SSE UI)
"""

import json
from typing import Any, Callable, Dict, List, Optional
from core.budget import BudgetExceededError, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider
from core.roles import RoleRegistry, RoleSpec
from core.storage import StorageBackend
from tools import AVAILABLE_TOOLS, TOOL_SCHEMAS


def _build_context_block(storage: StorageBackend, tenant_id: str) -> str:
    """Assemble strictly private tenant memories + shared sanitized global skills."""
    sections: List[str] = []

    tenant_mems = storage.get_tenant_memories(tenant_id)
    if tenant_mems:
        lines = "\n".join(f"  - {k}: {v}" for k, v in tenant_mems.items())
        sections.append(f"[PRIVATE TENANT MEMORY (tenant='{tenant_id}')]:\n{lines}")

    global_skills = storage.get_global_skills(limit=8)
    if global_skills:
        s_lines = "\n".join(
            f"  - [{s['category']}] When: {s['problem_pattern']} -> Do: {s['solution_rule']}"
            for s in global_skills
        )
        sections.append(f"[SHARED GLOBAL SKILLS (Anonymized Cross-Swarm Playbooks)]:\n{s_lines}")

    return ("\n\n" + "\n\n".join(sections)) if sections else ""


class WorkerAgent:
    """A specialized agent node with its own role, toolset, and budget enforcement."""

    def __init__(
        self,
        name: str,
        role: str,
        instructions: str,
        llm: LLMProvider,
        storage: StorageBackend,
        budget: BudgetTracker,
        bus: EventBus,
        tool_names: Optional[List[str]] = None,
        extra_tools: Optional[Dict[str, Callable[..., str]]] = None,
        extra_schemas: Optional[List[Dict[str, Any]]] = None,
        yield_on_tools: Optional[List[str]] = None,
    ) -> None:
        self.name = name
        self.role = role
        self.instructions = instructions
        self.llm = llm
        self.storage = storage
        self.budget = budget
        self.bus = bus
        self.tool_names = tool_names if tool_names is not None else list(AVAILABLE_TOOLS.keys())
        self.extra_tools = extra_tools or {}
        self.extra_schemas = extra_schemas or []
        self.yield_on_tools = set(yield_on_tools or [])

    def _get_tools_and_schemas(self, tenant_id: str) -> tuple[Dict[str, Callable[..., str]], List[Dict[str, Any]]]:
        """Bind tenant-scoped memory tools + global skill learning tool + selected tools."""
        tools_map: Dict[str, Callable[..., str]] = {}
        schemas: List[Dict[str, Any]] = []

        for name in self.tool_names:
            if name in AVAILABLE_TOOLS:
                tools_map[name] = AVAILABLE_TOOLS[name]

        for schema in TOOL_SCHEMAS:
            fn_name = schema["function"]["name"]
            if fn_name in self.tool_names:
                schemas.append(schema)

        # Override remember_fact & recall_memories so they use the multi-tenant SQLite storage
        if "remember_fact" in self.tool_names:
            def _tenant_remember(key: str, fact: str) -> str:
                self.storage.save_tenant_memory(tenant_id, key, fact)
                return f"Saved to tenant '{tenant_id}' private memory: {key} = {fact}"
            tools_map["remember_fact"] = _tenant_remember

        if "recall_memories" in self.tool_names:
            def _tenant_recall() -> str:
                mems = self.storage.get_tenant_memories(tenant_id)
                if not mems:
                    return f"No private memories stored for tenant '{tenant_id}'."
                return "\n".join(f"- {k}: {v}" for k, v in mems.items())
            tools_map["recall_memories"] = _tenant_recall

        # Add cross-swarm PrivacyAirlock skill-sharing tool
        def _share_global_skill(category: str, problem: str, solution: str) -> str:
            sanitized = self.storage.add_global_skill(category, problem, solution)
            self.bus.emit(
                "skill_learned",
                agent_name=self.name,
                tenant_id=tenant_id,
                **sanitized,
            )
            return (
                f"Sanitized & published to Global Skills: [{sanitized['category']}] "
                f"{sanitized['problem_pattern']} -> {sanitized['solution_rule']}"
            )

        tools_map["share_global_skill"] = _share_global_skill
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": "share_global_skill",
                    "description": (
                        "Publish a reusable, generic technical lesson or bug-fix pattern "
                        "to the shared Global Skills playbook (automatically scrubbed of private client data)."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Category (e.g., 'python', 'web_scraping', 'math').",
                            },
                            "problem": {
                                "type": "string",
                                "description": "Generic description of the technical problem or pitfall.",
                            },
                            "solution": {
                                "type": "string",
                                "description": "Generic reusable solution or best practice.",
                            },
                        },
                        "required": ["category", "problem", "solution"],
                    },
                },
            }
        )

        tools_map.update(self.extra_tools)
        schemas.extend(self.extra_schemas)
        return tools_map, schemas

    def execute_task(
        self,
        task_prompt: str,
        tenant_id: str = "default",
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Run the Think -> Act -> Observe loop for this agent under budget constraints."""
        tools_map, schemas = self._get_tools_and_schemas(tenant_id)
        system_prompt = (
            f"You are '{self.name}' ({self.role}).\n"
            f"{self.instructions}"
            f"{_build_context_block(self.storage, tenant_id)}"
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": task_prompt})

        self.bus.emit("agent_start", agent_name=self.name, tenant_id=tenant_id, task=task_prompt)
        last_error_tool: Optional[tuple[str, str]] = None

        while True:
            self.budget.check_and_increment_step(self.name)
            resp = self.llm.generate(messages=messages, tools=schemas)
            self.budget.record_tokens(self.name, resp.prompt_tokens, resp.completion_tokens)

            if not resp.tool_calls:
                answer = resp.content.strip()
                self.bus.emit(
                    "agent_finish",
                    agent_name=self.name,
                    tenant_id=tenant_id,
                    answer=answer,
                )
                self.storage.log_trace(
                    tenant_id, self.name, "agent_finish", {"answer": answer[:500]}
                )
                return answer

            if not resp.used_react_fallback and resp.raw_message is not None:
                messages.append(resp.raw_message)

            react_called = []
            react_observations = []

            for tc in resp.tool_calls:
                self.bus.emit(
                    "tool_call",
                    agent_name=self.name,
                    tenant_id=tenant_id,
                    tool=tc.name,
                    arguments=tc.arguments,
                )
                self.storage.log_trace(
                    tenant_id, self.name, "tool_call", {"tool": tc.name, "args": tc.arguments}
                )

                fn = tools_map.get(tc.name)
                if not fn:
                    result = f"Error: Unknown tool '{tc.name}'. Available: {list(tools_map)}"
                else:
                    try:
                        result = str(fn(**(tc.arguments or {})))
                    except BudgetExceededError:
                        raise
                    except Exception as exc:
                        result = f"Error running '{tc.name}': {exc}"

                # Auto-detect error recovery and distill a sanitized Global Skill!
                is_error = result.startswith("Error") or "STDERR:\nTraceback" in result
                if is_error:
                    last_error_tool = (tc.name, result[:200])
                elif last_error_tool and last_error_tool[0] == tc.name:
                    sanitized = self.storage.add_global_skill(
                        category=tc.name,
                        problem=f"Tool {tc.name} failed with: {last_error_tool[1]}",
                        solution=f"Resolved by calling {tc.name} with valid structure.",
                    )
                    self.bus.emit(
                        "skill_learned",
                        agent_name=self.name,
                        tenant_id=tenant_id,
                        **sanitized,
                    )
                    last_error_tool = None

                self.bus.emit(
                    "tool_result",
                    agent_name=self.name,
                    tenant_id=tenant_id,
                    tool=tc.name,
                    result=result,
                )

                if not resp.used_react_fallback:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.name,
                            "content": result,
                        }
                    )
                else:
                    react_called.append({"tool": tc.name, "args": tc.arguments})
                    react_observations.append(f"Observation from {tc.name}: {result}")

            if any(tc.name in self.yield_on_tools for tc in resp.tool_calls):
                return ""

            if resp.used_react_fallback:
                messages.append(
                    {"role": "assistant", "content": "\n".join(json.dumps(c) for c in react_called)}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "\n".join(react_observations)
                            + "\nUse the observation(s) above to continue or answer in plain text."
                        ),
                    }
                )


class OrchestratorSwarm:
    """Step 2 Architecture: Central Orchestrator delegating tasks to specialized Workers from RoleRegistry."""

    def __init__(
        self,
        llm: LLMProvider,
        storage: StorageBackend,
        budget: BudgetTracker,
        bus: EventBus,
        role_registry: Optional[RoleRegistry] = None,
        allowed_roles: Optional[List[str]] = None,
        directive: str = "",
        extra_tools: Optional[Dict[str, Callable[..., str]]] = None,
        extra_schemas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.llm = llm
        self.storage = storage
        self.budget = budget
        self.bus = bus
        self.role_registry = role_registry or RoleRegistry()
        self.allowed_roles = allowed_roles
        self.directive = directive.strip()
        self.extra_tools = extra_tools or {}
        self.extra_schemas = extra_schemas or []
        self.workers: Dict[str, WorkerAgent] = {}
        self.refresh_workers()

    def _active_role_specs(self) -> List[RoleSpec]:
        all_specs = self.role_registry.list_roles()
        if not self.allowed_roles:
            return all_specs
        allowed_set = {r.lower().strip() for r in self.allowed_roles}
        filtered = [s for s in all_specs if s.name in allowed_set]
        return filtered or all_specs

    def _build_worker(self, spec: RoleSpec) -> WorkerAgent:
        return WorkerAgent(
            name=spec.name,
            role=spec.title,
            instructions=spec.instructions,
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=self.bus,
            tool_names=spec.tool_names,
        )

    def refresh_workers(self) -> None:
        """Rebuild worker instances from the declarative RoleRegistry."""
        self.workers = {
            spec.name: self._build_worker(spec)
            for spec in self._active_role_specs()
        }

    def run(self, user_input: str, tenant_id: str = "default") -> str:
        """Run the Orchestrator -> Workers flow for a given tenant."""
        self.budget.reset()
        self.budget.tenant_id = tenant_id
        self.refresh_workers()

        def delegate_to_worker(worker_name: str, task: str) -> str:
            target = worker_name.lower().strip()
            worker = self.workers.get(target)
            if not worker:
                return f"Error: Unknown worker '{worker_name}'. Available workers: {list(self.workers)}"
            self.bus.emit(
                "delegation",
                agent_name="orchestrator",
                tenant_id=tenant_id,
                to_worker=worker.name,
                task=task,
            )
            return worker.execute_task(task, tenant_id=tenant_id)

        def _on_spawn(spec: RoleSpec) -> None:
            self.workers[spec.name] = self._build_worker(spec)
            self.bus.emit(
                "role_created",
                agent_name="orchestrator",
                tenant_id=tenant_id,
                role=spec.to_dict(),
            )

        spawn_fn, spawn_schema = self.role_registry.make_spawn_specialist_tool(on_spawn=_on_spawn)

        roster_lines = [
            f"- '{s.name}' ({s.title}): {s.instructions} [Tools: {', '.join(s.tool_names)}]"
            for s in self._active_role_specs()
        ]
        roster_text = "\n".join(roster_lines)

        delegate_schema = {
            "type": "function",
            "function": {
                "name": "delegate_to_worker",
                "description": (
                    f"Delegate a subtask to a specialized worker agent. "
                    f"Available workers: {list(self.workers.keys())}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "worker_name": {
                            "type": "string",
                            "description": f"Worker name (e.g. {', '.join(self.workers.keys())}).",
                        },
                        "task": {
                            "type": "string",
                            "description": "Clear, self-contained instructions for the worker.",
                        },
                    },
                    "required": ["worker_name", "task"],
                },
            },
        }

        orch_tools: Dict[str, Callable[..., str]] = {
            "delegate_to_worker": delegate_to_worker,
            "spawn_specialist": spawn_fn,
            **self.extra_tools,
        }
        orch_schemas: List[Dict[str, Any]] = [delegate_schema, spawn_schema, *self.extra_schemas]
        directive_block = f"\nSWARM MISSION DIRECTIVE:\n{self.directive}\n" if self.directive else ""

        orchestrator = WorkerAgent(
            name="orchestrator",
            role="Lead Swarm Coordinator",
            instructions=f"{CONFIG.orchestrator_prompt}{directive_block}\nACTIVE WORKER ROSTER:\n{roster_text}",
            llm=self.llm,
            storage=self.storage,
            budget=self.budget,
            bus=self.bus,
            tool_names=["remember_fact", "recall_memories"],
            extra_tools=orch_tools,
            extra_schemas=orch_schemas,
        )

        past_turns = self.storage.get_session_turns(tenant_id, limit=10)
        try:
            answer = orchestrator.execute_task(
                user_input,
                tenant_id=tenant_id,
                conversation_history=past_turns,
            )
            self.storage.append_session_turn(tenant_id, "user", user_input)
            self.storage.append_session_turn(tenant_id, "assistant", answer)
            return answer
        except BudgetExceededError as exc:
            msg = f"[Budget Guardrail Triggered] {exc}"
            self.bus.emit("budget_exceeded", agent_name="orchestrator", tenant_id=tenant_id, error=str(exc))
            return msg
