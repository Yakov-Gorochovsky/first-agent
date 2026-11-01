"""
core/swarm.py - Multi-Agent Orchestrator & Worker Harness.

Connects decoupled interfaces:
- LLMProvider (Ollama, Hugging Face, Groq, Gemini, OpenAI)
- StorageBackend (SQLite multi-tenant private memories + PrivacyAirlock global skills)
- BudgetTracker (step, token, and wall-time caps)
- EventBus (real-time telemetry for CLI and SSE UI)
"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.approval import ApprovalGate
from core.budget import BudgetExceededError, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider, LLMResponse, ToolCallRequest, strip_tool_call_text
from core.roles import RoleRegistry, RoleSpec
from core.security import TaintTracker, wrap_untrusted_content
from core.storage import StorageBackend
from core.tool_binding import bind_tools
from core.tool_runner import ToolRunner
from tools import AVAILABLE_TOOLS

SPECIALIST_GUIDANCE = (
    "\n\n[WORKING GUIDELINES]:\n"
    "1. Do your assigned part of the mission; build on earlier work instead of repeating it.\n"
    "2. Use a tool when it improves the result, and skip it when it does not.\n"
    "3. Base factual claims on what your tools returned, and say so when you could not verify something.\n"
    "4. Use `read_note` and `list_notes` for workspace files, never `run_python`.\n"
    "5. Put the complete result in your final reply, even if you also saved it to a note."
)
FINAL_ANSWER_PROMPT = {
    "role": "user",
    "content": "Tools are no longer available. Write your final answer now as plain text, using what you already have.",
}


COORDINATOR_NAMES = ("coordinator", "orchestrator")


def _budget_stop_message(exc: BudgetExceededError, observations: List[str]) -> str:
    if not observations:
        return f"[Continuation: {exc} Continue the workflow using existing findings or another specialist.]"
    findings = "\n\n".join(observations)
    return f"{findings}\n\n[Continuation: {exc} Returning gathered findings so the swarm can continue.]"


def _tool_result_messages(resp: LLMResponse, results: List[Tuple[ToolCallRequest, str]]) -> List[Dict[str, Any]]:
    """Format results the way the model called the tools: native messages or plain ReAct text."""
    if not resp.used_react_fallback:
        return [
            {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result}
            for call, result in results
        ]
    calls = "\n".join(json.dumps({"tool": call.name, "args": call.arguments}) for call, _ in results)
    observations = "\n".join(f"Observation from {call.name}: {result}" for call, result in results)
    prompt = f"{observations}\nUse the observation(s) above to continue or answer in plain text."
    return [{"role": "assistant", "content": calls}, {"role": "user", "content": prompt}]


def _build_context_block(storage: StorageBackend, tenant_id: str, include_skills: bool = True) -> str:
    """Assemble strictly private tenant memories + shared sanitized global skills."""
    sections: List[str] = []

    tenant_mems = storage.get_tenant_memories(tenant_id)
    if tenant_mems:
        lines = "\n".join(f"  - {k}: {v}" for k, v in tenant_mems.items())
        framed = wrap_untrusted_content(f"tenant_memory:{tenant_id}", lines)
        sections.append(f"[PRIVATE TENANT MEMORY (tenant='{tenant_id}')]:\n{framed}")

    if include_skills:
        global_skills = storage.get_global_skills(limit=5, approved_only=True)
        if global_skills:
            s_lines = "\n".join(
                f"  - [{s['category']}] When: {s['problem_pattern']} -> Do: {s['solution_rule']}"
                for s in global_skills
            )
            framed = wrap_untrusted_content("approved_global_skills", s_lines)
            sections.append(f"[SHARED GLOBAL SKILLS (Anonymized Cross-Swarm Playbooks)]:\n{framed}")

    return ("\n\n" + "\n\n".join(sections)) if sections else ""


def _get_compact_session_history(
    storage: StorageBackend, tenant_id: str, user_input: str
) -> List[Dict[str, str]]:
    """Return lean session history: full recent context on 'continue' prompts, compact on new missions."""
    clean_input = user_input.strip().lower()
    is_continuation = clean_input.startswith(("continue", "resume", "proceed", "go on")) or len(clean_input) < 24
    raw_turns = storage.get_session_turns(tenant_id, limit=6 if is_continuation else 2)
    if not raw_turns:
        return []
    if not is_continuation:
        # For a brand-new standalone mission, avoid re-injecting multi-thousand-token prior reports
        return []
    max_chars = 2000
    compact: List[Dict[str, str]] = []
    for t in raw_turns:
        content = str(t.get("content", ""))
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...[truncated prior turn]..."
        compact.append({"role": str(t.get("role", "user")), "content": content})
    return compact


def _synthesize_executive_fallback(
    llm: LLMProvider,
    user_input: str,
    findings: List[str],
    reason: str = "",
) -> str:
    """Perform a single tool-less synthesis call over collected specialist findings if budget expires mid-run."""
    joined_findings = "\n\n".join(findings)
    try:
        synth_messages = [
            {
                "role": "system",
                "content": (
                    "You are the Lead Swarm Synthesizer. Multiple specialist agents have completed their empirical "
                    "investigations and gathered the findings below. Synthesize all verified evidence, concrete artifacts, "
                    "and prioritized recommendations into a clear, comprehensive final report answering the user's request."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"ORIGINAL MISSION REQUEST:\n{user_input}\n\n"
                    f"GATHERED SPECIALIST FINDINGS:\n{joined_findings}\n\n"
                    "Produce the complete, structured executive synthesis now."
                ),
            },
        ]
        resp = llm.generate(messages=synth_messages, tools=None)
        if resp.content and resp.content.strip():
            return resp.content.strip()
    except Exception:
        pass
    suffix = f"\n\n[Note: {reason}]" if reason else ""
    return f"{joined_findings}{suffix}"


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
        max_steps: Optional[int] = None,
        taint_tracker: Optional[TaintTracker] = None,
        approval_gate: Optional[ApprovalGate] = None,
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
        self.max_steps = max_steps
        self.taint_tracker = taint_tracker or TaintTracker()
        self.approval_gate = approval_gate

    def _get_tools_and_schemas(self, tenant_id: str) -> tuple[Dict[str, Callable[..., str]], List[Dict[str, Any]]]:
        return bind_tools(
            self.tool_names, self.storage, self.bus, self.name, tenant_id, self.extra_tools, self.extra_schemas
        )

    def _initial_messages(
        self, task_prompt: str, tenant_id: str, history: Optional[List[Dict[str, str]]]
    ) -> List[Dict[str, Any]]:
        include_skills = self.name not in COORDINATOR_NAMES
        guidance = SPECIALIST_GUIDANCE if include_skills else ""
        context = _build_context_block(self.storage, tenant_id, include_skills=include_skills)
        system_prompt = f"You are '{self.name}' ({self.role}).\n{self.instructions}{guidance}{context}"
        return [{"role": "system", "content": system_prompt}, *(history or []), {"role": "user", "content": task_prompt}]

    def _must_synthesize(self, turn: int, observations: List[str]) -> bool:
        """Withdraw tools on a specialist's last step, so it answers instead of stopping mid-work."""
        limit = self.max_steps if self.max_steps and self.max_steps > 0 else self.budget.config.max_steps_per_agent
        return self.name not in COORDINATOR_NAMES and turn >= limit and bool(observations)

    def _finish(self, tenant_id: str, answer: str) -> str:
        self.bus.emit("agent_finish", agent_name=self.name, tenant_id=tenant_id, answer=answer)
        self.storage.log_trace(tenant_id, self.name, "agent_finish", {"answer": answer[:500]})
        return answer

    def _retry_final_answer(self, messages: List[Dict[str, Any]]) -> str:
        """Ask once more when a reply had no usable text, such as tool markup with no tools offered."""
        resp = self.llm.generate(messages=[*messages, FINAL_ANSWER_PROMPT], tools=None)
        self.budget.record_tokens(self.name, resp.prompt_tokens, resp.completion_tokens)
        return strip_tool_call_text(resp.content)

    def _final_text(self, resp: LLMResponse, messages: List[Dict[str, Any]], observations: List[str]) -> str:
        answer = strip_tool_call_text(resp.content)
        if answer or not observations:
            return answer
        return self._retry_final_answer(messages) or "\n\n".join(observations)

    def _yield_summary(self, resp: LLMResponse) -> Optional[str]:
        """Return a summary if this turn called a tool that hands control back to the topology."""
        yielding = [call for call in resp.tool_calls if call.name in self.yield_on_tools]
        if not yielding:
            return None
        if resp.content and resp.content.strip():
            return resp.content.strip()
        args = yielding[0].arguments if isinstance(yielding[0].arguments, dict) else {}
        return str(args.get("final_answer") or args.get("message") or "")[:500]

    def execute_task(
        self,
        task_prompt: str,
        tenant_id: str = "default",
        conversation_history: Optional[List[Dict[str, str]]] = None,
    ) -> str:
        """Run the Think -> Act -> Observe loop for this agent under budget constraints."""
        messages = self._initial_messages(task_prompt, tenant_id, conversation_history)
        self.bus.emit("agent_start", agent_name=self.name, tenant_id=tenant_id, task=task_prompt)
        runner = ToolRunner(
            self.name, tenant_id, self.budget, self.bus, self.storage, self.taint_tracker, self.approval_gate
        )
        observations: List[str] = []
        turn = 0

        while True:
            turn += 1
            tools_map, schemas = self._get_tools_and_schemas(tenant_id)
            active_schemas = None if self._must_synthesize(turn, observations) else schemas

            try:
                self.budget.check_and_increment_step(self.name, max_steps=self.max_steps)
            except BudgetExceededError as exc:
                return self._finish(tenant_id, _budget_stop_message(exc, observations))

            call_messages = messages if active_schemas else [*messages, FINAL_ANSWER_PROMPT]
            resp = self.llm.generate(messages=call_messages, tools=active_schemas)
            self.budget.record_tokens(self.name, resp.prompt_tokens, resp.completion_tokens)

            if not resp.tool_calls:
                return self._finish(tenant_id, self._final_text(resp, messages, observations))

            if not resp.used_react_fallback and resp.raw_message is not None:
                messages.append(resp.raw_message)

            results = [(call, runner.run(call, tools_map)) for call in resp.tool_calls]
            observations.extend(f"[{call.name}]: {result[:600]}" for call, result in results)

            summary = self._yield_summary(resp)
            if summary is not None:
                self._finish(tenant_id, summary)
                return ""

            messages.extend(_tool_result_messages(resp, results))


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
        taint_tracker: Optional[TaintTracker] = None,
        approval_gate: Optional[ApprovalGate] = None,
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
        self.taint_tracker = taint_tracker or TaintTracker()
        self.approval_gate = approval_gate
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
            max_steps=spec.effective_max_steps,
            taint_tracker=self.taint_tracker,
            approval_gate=self.approval_gate,
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
        self.taint_tracker = TaintTracker(mission_id=f"orch_{tenant_id}")
        self.refresh_workers()

        mission_findings: List[str] = []

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
            enriched_task = task
            if mission_findings:
                prior_summary = "\n\n".join(mission_findings[-4:])
                enriched_task = (
                    f"{task}\n\n"
                    f"[PRIOR SPECIALIST FINDINGS IN THIS MISSION]:\n{prior_summary}"
                )
            worker_out = worker.execute_task(enriched_task, tenant_id=tenant_id)
            if worker_out:
                mission_findings.append(f"[{worker.name}]:\n{worker_out[:1500]}")
            return worker_out

        def _on_spawn(spec: RoleSpec) -> None:
            self.workers[spec.name] = self._build_worker(spec)
            self.bus.emit(
                "role_created",
                agent_name="orchestrator",
                tenant_id=tenant_id,
                role=spec.to_dict(),
            )

        spawn_fn, spawn_schema = self.role_registry.make_spawn_specialist_tool(
            on_spawn=_on_spawn,
            active_roster_names=lambda: list(self.workers.keys()),
        )

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
            max_steps=CONFIG.max_coordinator_steps,
        )

        past_turns = _get_compact_session_history(self.storage, tenant_id, user_input)
        try:
            answer = orchestrator.execute_task(
                user_input,
                tenant_id=tenant_id,
                conversation_history=past_turns,
            )
            if (
                mission_findings
                and (
                    "[Continuation:" in answer
                    or answer.lstrip().startswith("[delegate_to_worker]:")
                )
            ):
                answer = _synthesize_executive_fallback(
                    self.llm, user_input, mission_findings
                )
            self.storage.append_session_turn(tenant_id, "user", user_input)
            self.storage.append_session_turn(tenant_id, "assistant", answer)
            return answer
        except BudgetExceededError as exc:
            self.bus.emit("budget_exceeded", agent_name="orchestrator", tenant_id=tenant_id, error=str(exc))
            if mission_findings:
                synth = _synthesize_executive_fallback(
                    self.llm, user_input, mission_findings, reason=str(exc)
                )
                self.storage.append_session_turn(tenant_id, "user", user_input)
                self.storage.append_session_turn(tenant_id, "assistant", synth)
                return synth
            msg = f"[Budget Guardrail Triggered] {exc}"
            return msg
