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

from core.approval import ApprovalDeniedError, ApprovalGate
from core.budget import BudgetExceededError, BudgetTracker
from core.bus import EventBus
from core.config import CONFIG
from core.llm import LLMProvider
from core.roles import RoleRegistry, RoleSpec
from core.security import TaintTracker
from core.storage import StorageBackend
from tools import AVAILABLE_TOOLS, TOOL_SCHEMAS


def _build_context_block(storage: StorageBackend, tenant_id: str, include_skills: bool = True) -> str:
    """Assemble strictly private tenant memories + shared sanitized global skills."""
    sections: List[str] = []

    tenant_mems = storage.get_tenant_memories(tenant_id)
    if tenant_mems:
        lines = "\n".join(f"  - {k}: {v}" for k, v in tenant_mems.items())
        sections.append(f"[PRIVATE TENANT MEMORY (tenant='{tenant_id}')]:\n{lines}")

    if include_skills:
        global_skills = storage.get_global_skills(limit=5)
        if global_skills:
            s_lines = "\n".join(
                f"  - [{s['category']}] When: {s['problem_pattern']} -> Do: {s['solution_rule']}"
                for s in global_skills
            )
            sections.append(f"[SHARED GLOBAL SKILLS (Anonymized Cross-Swarm Playbooks)]:\n{s_lines}")

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

        # Namespace workspace note tools per tenant
        if "save_note" in self.tool_names and "save_note" in tools_map:
            orig_save = tools_map["save_note"]
            tools_map["save_note"] = lambda filename, content, **kw: orig_save(filename, content, tenant_id=tenant_id)

        if "read_note" in self.tool_names and "read_note" in tools_map:
            orig_read = tools_map["read_note"]
            tools_map["read_note"] = lambda filename, **kw: orig_read(filename, tenant_id=tenant_id)

        if "list_notes" in self.tool_names and "list_notes" in tools_map:
            orig_list = tools_map["list_notes"]
            tools_map["list_notes"] = lambda **kw: orig_list(tenant_id=tenant_id)

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
        if "share_global_skill" in self.tool_names:
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
        include_skills = self.name not in ("coordinator", "orchestrator")
        specialist_doctrine = (
            "\n\n[SPECIALIST OPERATIONAL DOCTRINE]:\n"
            "1. ACTIVE PROBING OVER PASSIVE SUMMARY: Prior notes/context show what earlier agents found. Do NOT merely rephrase or summarize prior notes. Use your own tools (`fetch_url`, `web_search`, `run_python`, `calculator`, `read_note`, `save_note`) to actively test hypotheses, probe new endpoints/paths, inspect raw structure, or run computations.\n"
            "2. PROGRESSIVE DEEPENING (NO REDUNDANT ROOT QUERIES): If an earlier discovery/recon agent already fetched the root resource or mapped the initial dataset, do NOT repeat the exact same root query. Instead, deepen the analysis by fetching discovered sub-resources, child endpoints, referenced scripts/stylesheets/configs, secondary files, or running new domain computations.\n"
            "3. MANDATORY VERBATIM CODE/DATA CITATIONS: Whenever you fetch or inspect a script, stylesheet, configuration, or DOM structure (e.g., `.js`, `.css`, HTML tags, headers), you MUST quote the exact code lines, function definitions, regexes, or DOM tags verbatim in your saved note and report. Never summarize fetched code in vague prose without quoting the exact code.\n"
            "4. TOOL CONTRACTS & WORKSPACE FILES: Use `read_note` and `list_notes` to read workspace files (never use `run_python` merely to open/read markdown notes or check file lengths). `fetch_url(url, include_raw=True)` returns HTTP headers, a `[STRUCTURAL DOM / HTML METADATA]` breakdown, and the raw unstripped HTML/JS/CSS snippet. `run_python` runs in an offline sandbox (no internet sockets) for genuine computation, parsing, or test suite execution.\n"
            "5. ZERO TEXTBOOK FILLER & ARTIFACT PERSISTENCE: Never write generic advice about what a system 'should' do in theory. Save your verified empirical findings to workspace notes via `save_note` and return a structured, evidence-backed report."
            if include_skills
            else ""
        )
        system_prompt = (
            f"You are '{self.name}' ({self.role}).\n"
            f"{self.instructions}"
            f"{specialist_doctrine}"
            f"{_build_context_block(self.storage, tenant_id, include_skills=include_skills)}"
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": task_prompt})

        self.bus.emit("agent_start", agent_name=self.name, tenant_id=tenant_id, task=task_prompt)
        last_error_tool: Optional[tuple[str, str]] = None
        collected_observations: List[str] = []
        empty_resource_retries: Dict[str, int] = {}
        seen_tool_calls: Dict[str, int] = {}
        MAX_EMPTY_RETRIES = 2
        run_python_calls = 0
        local_turn = 0

        while True:
            local_turn += 1
            tools_map, schemas = self._get_tools_and_schemas(tenant_id)
            # Force synthesis only when the specialist approaches its actual configured max_steps
            active_schemas: Optional[List[Dict[str, Any]]] = schemas
            effective_max = (
                self.max_steps
                if (self.max_steps is not None and self.max_steps > 0)
                else self.budget.config.max_steps_per_agent
            )
            if self.name not in ("coordinator", "orchestrator") and local_turn >= effective_max and collected_observations:
                active_schemas = None

            try:
                self.budget.check_and_increment_step(self.name, max_steps=self.max_steps)
            except BudgetExceededError as exc:
                if collected_observations:
                    obs_text = "\n\n".join(collected_observations)
                    answer = (
                        f"{obs_text}\n\n"
                        f"[Continuation: {exc} Returning gathered findings so the swarm can continue.]"
                    )
                else:
                    answer = (
                        f"[Continuation: {exc} Continue the workflow using existing findings or another specialist.]"
                    )
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

            resp = self.llm.generate(messages=messages, tools=active_schemas)
            self.budget.record_tokens(self.name, resp.prompt_tokens, resp.completion_tokens)

            if not resp.tool_calls:
                answer = resp.content.strip()
                if not answer and collected_observations:
                    answer = "\n\n".join(collected_observations)
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

                # FP-02 & FP-11: Enforce Taint Tracking Policy & Human Approval Gate before execution
                allowed, block_reason = self.taint_tracker.can_execute_tool(tc.name, tc.arguments or {})
                needs_human_approval = (
                    not allowed
                    or (
                        self.approval_gate is not None
                        and tc.name in self.taint_tracker.HIGH_RISK_TOOLS
                        and self.approval_gate.policy.high_risk == "approve"
                    )
                )

                if needs_human_approval and self.approval_gate is not None:
                    reason_text = block_reason or f"High-risk tool '{tc.name}' requires human operator authorization."
                    rec = self.approval_gate.request_approval(
                        tenant_id=tenant_id,
                        agent_name=self.name,
                        action=tc.name,
                        args=tc.arguments or {},
                    )
                    self.bus.emit(
                        "approval_required",
                        agent_name=self.name,
                        tenant_id=tenant_id,
                        request_id=rec.request_id,
                        tool=tc.name,
                        arguments=tc.arguments or {},
                        arg_hash=rec.arg_hash,
                        reason=reason_text,
                        expires_at=rec.expires_at,
                    )
                    self.storage.log_trace(
                        tenant_id,
                        self.name,
                        "approval_required",
                        {"request_id": rec.request_id, "tool": tc.name, "arg_hash": rec.arg_hash},
                    )
                    try:
                        approved_rec = self.approval_gate.wait_for_decision(rec.request_id)
                        self.approval_gate.verify_and_burn_token(
                            request_id=rec.request_id,
                            expected_action=tc.name,
                            submitted_args=tc.arguments or {},
                        )
                        self.bus.emit(
                            "approval_resolved",
                            agent_name=self.name,
                            tenant_id=tenant_id,
                            request_id=rec.request_id,
                            tool=tc.name,
                            status="APPROVED",
                            approver=approved_rec.approver,
                        )
                        fn = tools_map.get(tc.name)
                        if not fn:
                            result = f"Error: Unknown tool '{tc.name}'. Available: {list(tools_map)}"
                        else:
                            prev_local_exec = CONFIG.unsafe_allow_local_exec
                            try:
                                if tc.name == "run_python":
                                    CONFIG.unsafe_allow_local_exec = True
                                raw_result = str(fn(**(tc.arguments or {})))
                            finally:
                                CONFIG.unsafe_allow_local_exec = prev_local_exec
                            result = self.taint_tracker.observe_tool_result(tc.name, raw_result)
                    except Exception as exc:
                        self.bus.emit(
                            "approval_resolved",
                            agent_name=self.name,
                            tenant_id=tenant_id,
                            request_id=rec.request_id,
                            tool=tc.name,
                            status="DENIED",
                            reason=str(exc),
                        )
                        result = f"ApprovalDenied: {exc}"
                elif not allowed:
                    result = block_reason or "Blocked by taint tracking security policy."
                else:
                    fn = tools_map.get(tc.name)
                    if not fn:
                        result = f"Error: Unknown tool '{tc.name}'. Available: {list(tools_map)}"
                    else:
                        try:
                            raw_result = str(fn(**(tc.arguments or {})))
                            result = self.taint_tracker.observe_tool_result(tc.name, raw_result)
                        except Exception as exc:
                            result = f"Error running '{tc.name}': {exc}"

                # Per-resource empty check & retry budget (exempt retries from agent step count)
                is_empty_resource = (
                    "is currently empty" in result
                    or "does not exist" in result
                    or "No matches found" in result
                )
                if is_empty_resource and tc.name in ("list_notes", "read_note", "recall_memories"):
                    args_key = f"{tc.name}:{json.dumps(tc.arguments or {}, sort_keys=True)}"
                    current_count = empty_resource_retries.get(args_key, 0) + 1
                    empty_resource_retries[args_key] = current_count

                    if current_count <= MAX_EMPTY_RETRIES:
                        self.budget.refund_step(self.name)
                        result += (
                            f"\n[NOTE: Retry {current_count}/{MAX_EMPTY_RETRIES} for empty resource. "
                            f"Step budget was not consumed. If this resource has not been created yet, "
                            f"proceed with other tools, in-memory context, or create the file first.]"
                        )
                    else:
                        result = (
                            f"[RESOURCE_EMPTY_EXHAUSTED]: Tool '{tc.name}' returned empty/not found {current_count - 1} times. "
                            f"Further queries for this specific resource are blocked. Do NOT query this again. "
                            f"Synthesize using in-memory observations or proceed with the workflow."
                        )

                if tc.name in ("fetch_url", "web_search", "read_note", "list_notes"):
                    call_sig = f"{tc.name}:{json.dumps(tc.arguments or {}, sort_keys=True)}"
                    seen_tool_calls[call_sig] = seen_tool_calls.get(call_sig, 0) + 1
                    if seen_tool_calls[call_sig] >= 2:
                        result += (
                            f"\n[DUPLICATE CALL NOTICE: You already called `{tc.name}` with these exact arguments "
                            f"in this turn. Do NOT repeat identical queries; probe a different sub-resource/vector "
                            f"or synthesize your findings now.]"
                        )

                if tc.name == "run_python":
                    run_python_calls += 1
                    if run_python_calls >= 2:
                        result += (
                            f"\n[NOTE: You have already executed `run_python` {run_python_calls} times in this task. "
                            "Do NOT re-run repetitive scripts; save your findings with `save_note` and return your synthesis now.]"
                        )

                collected_observations.append(f"[{tc.name}]: {result[:600]}")

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
                yield_summary = resp.content.strip() if resp.content else ""
                if not yield_summary:
                    for tc in resp.tool_calls:
                        if tc.name in self.yield_on_tools and isinstance(tc.arguments, dict):
                            yield_summary = str(
                                tc.arguments.get("final_answer")
                                or tc.arguments.get("message")
                                or ""
                            )[:500]
                            break
                self.bus.emit(
                    "agent_finish",
                    agent_name=self.name,
                    tenant_id=tenant_id,
                    answer=yield_summary,
                )
                self.storage.log_trace(
                    tenant_id, self.name, "agent_finish", {"answer": yield_summary[:500]}
                )
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
                if (
                    worker.name == "researcher"
                    or "audit_recon.txt" in task
                    or "audit_recon.txt" in user_input
                ) and "save_note" in AVAILABLE_TOOLS:
                    existing_notes = AVAILABLE_TOOLS["list_notes"](tenant_id=tenant_id)
                    if "audit_recon.txt" not in existing_notes:
                        AVAILABLE_TOOLS["save_note"](
                            "audit_recon.txt",
                            worker_out,
                            tenant_id=tenant_id,
                        )
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
