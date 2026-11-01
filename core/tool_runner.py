"""
core/tool_runner.py - Runs one agent's tool calls under the security policy.

Every call goes through the same steps: announce it, decide whether policy allows it, ask a human if
required, run it, then add guidance for the model when a call is empty or repetitive.
"""

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from core.approval import ApprovalGate
from core.budget import BudgetTracker
from core.bus import EventBus
from core.llm import ToolCallRequest
from core.security import TaintTracker
from core.storage import StorageBackend

ToolMap = Dict[str, Callable[..., str]]

MAX_EMPTY_RETRIES = 2
EMPTY_RESOURCE_MARKERS = ("is currently empty", "does not exist", "No matches found")
EMPTY_AWARE_TOOLS = ("list_notes", "read_note", "recall_memories")
DUPLICATE_AWARE_TOOLS = ("fetch_url", "web_search", "read_note", "list_notes")


def _call_signature(call: ToolCallRequest) -> str:
    return f"{call.name}:{json.dumps(call.arguments or {}, sort_keys=True)}"


@dataclass
class ToolRunner:
    """Holds per-task call history, so retry and duplicate guidance stays within one agent's task."""

    agent_name: str
    tenant_id: str
    budget: BudgetTracker
    bus: EventBus
    storage: StorageBackend
    taint_tracker: TaintTracker
    approval_gate: Optional[ApprovalGate] = None
    empty_retries: Dict[str, int] = field(default_factory=dict)
    seen_calls: Dict[str, int] = field(default_factory=dict)
    python_calls: int = 0

    def run(self, call: ToolCallRequest, tools_map: ToolMap) -> str:
        self._emit("tool_call", tool=call.name, arguments=call.arguments)
        self._trace("tool_call", {"tool": call.name, "args": call.arguments})
        result = self._with_guidance(call, self._authorized_result(call, tools_map))
        self._emit("tool_result", tool=call.name, result=result)
        return result

    def _emit(self, event_type: str, **data: object) -> None:
        self.bus.emit(event_type, agent_name=self.agent_name, tenant_id=self.tenant_id, **data)

    def _trace(self, event_type: str, data: Dict[str, object]) -> None:
        self.storage.log_trace(self.tenant_id, self.agent_name, event_type, data)

    def _authorized_result(self, call: ToolCallRequest, tools_map: ToolMap) -> str:
        allowed, block_reason = self.taint_tracker.can_execute_tool(call.name, call.arguments or {})
        gate = self.approval_gate
        policy_requires_human = (
            gate is not None
            and call.name in self.taint_tracker.HIGH_RISK_TOOLS
            and gate.policy.high_risk == "approve"
        )
        if gate is not None and (policy_requires_human or not allowed):
            return self._run_with_approval(gate, call, tools_map, block_reason)
        if not allowed:
            return block_reason or "Blocked by taint tracking security policy."
        return self._invoke(call, tools_map)

    def _invoke(self, call: ToolCallRequest, tools_map: ToolMap) -> str:
        fn = tools_map.get(call.name)
        if not fn:
            return f"Error: Unknown tool '{call.name}'. Available: {list(tools_map)}"
        try:
            return self.taint_tracker.observe_tool_result(call.name, str(fn(**(call.arguments or {}))))
        except Exception as exc:
            return f"Error running '{call.name}': {exc}"

    def _run_with_approval(
        self, gate: ApprovalGate, call: ToolCallRequest, tools_map: ToolMap, block_reason: Optional[str]
    ) -> str:
        args = call.arguments or {}
        rec = gate.request_approval(tenant_id=self.tenant_id, agent_name=self.agent_name, action=call.name, args=args)
        self._emit(
            "approval_required",
            request_id=rec.request_id,
            tool=call.name,
            arguments=args,
            arg_hash=rec.arg_hash,
            reason=block_reason or f"High-risk tool '{call.name}' requires human operator authorization.",
            expires_at=rec.expires_at,
        )
        self._trace("approval_required", {"request_id": rec.request_id, "tool": call.name, "arg_hash": rec.arg_hash})
        try:
            approved = gate.wait_for_decision(rec.request_id)
            gate.verify_and_burn_token(request_id=rec.request_id, expected_action=call.name, submitted_args=args)
        except Exception as exc:
            self._emit(
                "approval_resolved", request_id=rec.request_id, tool=call.name, status="DENIED", reason=str(exc)
            )
            return f"ApprovalDenied: {exc}"
        self._emit(
            "approval_resolved",
            request_id=rec.request_id,
            tool=call.name,
            status="APPROVED",
            approver=approved.approver,
        )
        return self._invoke(call, tools_map)

    def _with_guidance(self, call: ToolCallRequest, result: str) -> str:
        result = self._apply_empty_retry_budget(call, result)
        if call.name in DUPLICATE_AWARE_TOOLS:
            signature = _call_signature(call)
            self.seen_calls[signature] = self.seen_calls.get(signature, 0) + 1
            if self.seen_calls[signature] >= 2:
                result += (
                    f"\n[DUPLICATE CALL NOTICE: You already called `{call.name}` with these exact arguments. "
                    "Do not repeat it; try something different or give your result now.]"
                )
        if call.name == "run_python":
            self.python_calls += 1
            if self.python_calls >= 2:
                result += (
                    f"\n[NOTE: You have run `run_python` {self.python_calls} times in this task. "
                    "Avoid repeating similar scripts; give your result if you have what you need.]"
                )
        return result

    def _apply_empty_retry_budget(self, call: ToolCallRequest, result: str) -> str:
        """Looking for something not created yet shouldn't cost steps, up to a limit."""
        is_empty = any(marker in result for marker in EMPTY_RESOURCE_MARKERS)
        if not is_empty or call.name not in EMPTY_AWARE_TOOLS:
            return result
        signature = _call_signature(call)
        count = self.empty_retries.get(signature, 0) + 1
        self.empty_retries[signature] = count
        if count > MAX_EMPTY_RETRIES:
            return (
                f"[RESOURCE_EMPTY_EXHAUSTED]: Tool '{call.name}' returned empty/not found {count - 1} times. "
                "Do NOT query this again. Continue with what you already have."
            )
        self.budget.refund_step(self.agent_name)
        return (
            f"{result}\n[NOTE: Retry {count}/{MAX_EMPTY_RETRIES} for empty resource. Step budget was not consumed. "
            "If this resource has not been created yet, use other tools or create it first.]"
        )
