"""
core/approval.py - FP-11 (Lite): Single-Process Human Approval Gates.

Architectural Purpose:
Enforces mandatory human authorization before high-risk actions (e.g. code execution,
file modifications, external communications) are executed by autonomous agents.

Guarantees:
1. Strict Action & Argument Binding: Approvals cryptographically bind action name + SHA-256 of raw arguments.
2. Anti-Tampering: Rejects calls if arguments were altered after human approval was granted.
3. Single-Use Tokens: Approvals are burned immediately upon consumption to prevent replay attacks.
4. Expiration & Timeout: Approvals expire after a configured TTL; pending approvals timeout and default to DENY (fail closed).
5. Append-Only Audit Trail: Every approval request, grant, denial, timeout, and tampering attempt is logged.
"""

import hashlib
import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("neural_swarm.approval")


class ApprovalDeniedError(RuntimeError):
    """Raised when an action is denied by the human approver or times out."""


class ApprovalTamperingError(RuntimeError):
    """Raised when action arguments do not match the approved SHA-256 hash."""


class ApprovalExpiredError(RuntimeError):
    """Raised when an approval token has passed its expiration timestamp."""


class ApprovalReplayError(RuntimeError):
    """Raised when a single-use approval token is submitted more than once."""


@dataclass
class ApprovalPolicy:
    """Policy mapping tool risk classes to execution requirements."""
    low_risk: str = "auto"       # "auto" | "approve" | "deny"
    medium_risk: str = "auto"    # "auto" | "approve" | "deny"
    high_risk: str = "approve"   # "auto" | "approve" | "deny"
    default_timeout_sec: float = 30.0
    token_ttl_sec: float = 300.0


@dataclass
class ApprovalRecord:
    """Approval request and authorization token."""
    request_id: str
    tenant_id: str
    agent_name: str
    action: str
    raw_args: Dict[str, Any]
    arg_hash: str
    created_at: float
    expires_at: float
    status: str = "PENDING"  # "PENDING" | "APPROVED" | "DENIED" | "EXPIRED" | "USED"
    approver: Optional[str] = None
    reason: Optional[str] = None
    approval_token: Optional[str] = None


@dataclass
class AuditLogEntry:
    """Immutable audit trail entry."""
    timestamp: float
    tenant_id: str
    agent_name: str
    action: str
    arg_hash: str
    status: str
    approver: Optional[str] = None
    detail: Optional[str] = None


def compute_arg_hash(args: Dict[str, Any]) -> str:
    """Deterministic SHA-256 hash of serialized tool arguments."""
    serialized = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class ApprovalGate:
    """
    In-memory Human Approval Gate managing interactive authorizations, timeouts, and replay prevention.
    """

    def __init__(self, policy: Optional[ApprovalPolicy] = None) -> None:
        self.policy = policy or ApprovalPolicy()
        self._requests: Dict[str, ApprovalRecord] = {}
        self._tokens: Dict[str, ApprovalRecord] = {}  # token_id -> record
        self._audit_log: List[AuditLogEntry] = []
        self._lock = threading.Lock()
        self._events: Dict[str, threading.Event] = {}

    def _log_audit(
        self,
        tenant_id: str,
        agent_name: str,
        action: str,
        arg_hash: str,
        status: str,
        approver: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        entry = AuditLogEntry(
            timestamp=time.time(),
            tenant_id=tenant_id,
            agent_name=agent_name,
            action=action,
            arg_hash=arg_hash,
            status=status,
            approver=approver,
            detail=detail,
        )
        self._audit_log.append(entry)
        logger.info(
            json.dumps({
                "event": "approval_audit",
                "tenant_id": tenant_id,
                "action": action,
                "status": status,
                "approver": approver,
                "arg_hash": arg_hash,
            })
        )

    def request_approval(
        self,
        tenant_id: str,
        agent_name: str,
        action: str,
        args: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> ApprovalRecord:
        """
        Creates an approval request and optionally blocks until approved or timed out.
        """
        arg_hash = compute_arg_hash(args)
        request_id = f"appr_{secrets.token_urlsafe(32)}"
        now = time.time()
        timeout_sec = timeout if timeout is not None else self.policy.default_timeout_sec

        record = ApprovalRecord(
            request_id=request_id,
            tenant_id=tenant_id,
            agent_name=agent_name,
            action=action,
            raw_args=args,
            arg_hash=arg_hash,
            created_at=now,
            expires_at=now + timeout_sec,
            status="PENDING",
        )

        evt = threading.Event()
        with self._lock:
            self._requests[request_id] = record
            self._events[request_id] = evt

        self._log_audit(
            tenant_id=tenant_id,
            agent_name=agent_name,
            action=action,
            arg_hash=arg_hash,
            status="PENDING",
            detail=f"Requested approval with timeout={timeout_sec}s",
        )

        return record

    def wait_for_decision(self, request_id: str, timeout: Optional[float] = None) -> ApprovalRecord:
        """Block the calling worker thread until human resolves or timeout expires."""
        with self._lock:
            evt = self._events.get(request_id)
            record = self._requests.get(request_id)

        if not evt or not record:
            raise ApprovalDeniedError(f"Approval request '{request_id}' not found.")

        eff_timeout = timeout if timeout is not None else self.policy.default_timeout_sec
        signaled = evt.wait(timeout=eff_timeout)

        with self._lock:
            if not signaled or record.status == "PENDING":
                record.status = "EXPIRED"
                self._log_audit(
                    tenant_id=record.tenant_id,
                    agent_name=record.agent_name,
                    action=record.action,
                    arg_hash=record.arg_hash,
                    status="EXPIRED",
                    detail="Approval timed out before human decision.",
                )
                raise ApprovalDeniedError(f"Approval request '{request_id}' timed out after {eff_timeout}s.")

            if record.status != "APPROVED":
                raise ApprovalDeniedError(
                    f"Approval request '{request_id}' was {record.status}: {record.reason or 'No reason provided'}."
                )

            return record

    def approve(
        self,
        request_id: str,
        approver: str,
        reason: Optional[str] = None,
    ) -> ApprovalRecord:
        """Grant approval by a designated human operator."""
        with self._lock:
            record = self._requests.get(request_id)
            if not record:
                raise ValueError(f"Request '{request_id}' not found.")

            if record.status != "PENDING":
                raise ValueError(f"Request '{request_id}' is already {record.status}.")

            if time.time() > record.expires_at:
                record.status = "EXPIRED"
                self._log_audit(record.tenant_id, record.agent_name, record.action, record.arg_hash, "EXPIRED")
                raise ApprovalExpiredError(f"Request '{request_id}' has already expired.")

            token = f"tok_{secrets.token_urlsafe(32)}"
            record.status = "APPROVED"
            record.approver = approver
            record.reason = reason
            record.approval_token = token
            self._tokens[request_id] = record
            self._tokens[token] = record

            evt = self._events.get(request_id)
            if evt:
                evt.set()

            self._log_audit(
                tenant_id=record.tenant_id,
                agent_name=record.agent_name,
                action=record.action,
                arg_hash=record.arg_hash,
                status="APPROVED",
                approver=approver,
                detail=reason,
            )
            return record

    def deny(
        self,
        request_id: str,
        approver: str,
        reason: Optional[str] = None,
    ) -> ApprovalRecord:
        """Deny approval request."""
        with self._lock:
            record = self._requests.get(request_id)
            if not record:
                raise ValueError(f"Request '{request_id}' not found.")

            record.status = "DENIED"
            record.approver = approver
            record.reason = reason or "Denied by operator."
            self._tokens[request_id] = record

            evt = self._events.get(request_id)
            if evt:
                evt.set()

            self._log_audit(
                tenant_id=record.tenant_id,
                agent_name=record.agent_name,
                action=record.action,
                arg_hash=record.arg_hash,
                status="DENIED",
                approver=approver,
                detail=reason,
            )
            return record

    def verify_and_burn_token(
        self,
        request_id: str,
        expected_action: str,
        submitted_args: Dict[str, Any],
        expected_approver: Optional[str] = None,
    ) -> None:
        """
        Verify that an approval token is valid, unexpired, matches raw arguments, and burn it (single-use).
        """
        with self._lock:
            record = self._tokens.get(request_id) or self._requests.get(request_id)
            if not record:
                self._log_audit("unknown", "unknown", expected_action, compute_arg_hash(submitted_args), "REJECTED_UNKNOWN_TOKEN")
                raise ApprovalDeniedError(f"Invalid or unknown approval token '{request_id}'.")

            if record.status == "USED":
                self._log_audit(record.tenant_id, record.agent_name, expected_action, record.arg_hash, "REPLAY_ATTEMPT", approver=record.approver)
                raise ApprovalReplayError(f"Approval token '{request_id}' has already been used.")

            if record.status != "APPROVED":
                raise ApprovalDeniedError(f"Token '{request_id}' is not in APPROVED state (current: {record.status}).")

            if time.time() > record.expires_at:
                record.status = "EXPIRED"
                self._log_audit(record.tenant_id, record.agent_name, expected_action, record.arg_hash, "EXPIRED_TOKEN")
                raise ApprovalExpiredError(f"Approval token '{request_id}' has expired.")

            if record.action != expected_action:
                self._log_audit(record.tenant_id, record.agent_name, expected_action, record.arg_hash, "ACTION_MISMATCH")
                raise ApprovalTamperingError(
                    f"Approval token '{request_id}' action mismatch: approved for '{record.action}', submitted '{expected_action}'."
                )

            current_arg_hash = compute_arg_hash(submitted_args)
            if record.arg_hash != current_arg_hash:
                self._log_audit(
                    record.tenant_id,
                    record.agent_name,
                    expected_action,
                    current_arg_hash,
                    "TAMPERED_ARGS",
                    approver=record.approver,
                    detail=f"Expected hash {record.arg_hash}, received {current_arg_hash}",
                )
                raise ApprovalTamperingError(
                    f"Arguments tampered after approval! Expected hash {record.arg_hash}, got {current_arg_hash}."
                )

            if expected_approver and record.approver != expected_approver:
                self._log_audit(record.tenant_id, record.agent_name, expected_action, record.arg_hash, "WRONG_APPROVER")
                raise ApprovalDeniedError(
                    f"Approver mismatch: expected '{expected_approver}', approved by '{record.approver}'."
                )

            # Burn token
            record.status = "USED"
            self._log_audit(
                record.tenant_id,
                record.agent_name,
                expected_action,
                record.arg_hash,
                "CONSUMED",
                approver=record.approver,
            )

    def get_audit_trail(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(e) for e in self._audit_log[-limit:]]


GLOBAL_APPROVAL_GATE = ApprovalGate()
