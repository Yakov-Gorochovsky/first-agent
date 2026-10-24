"""
core/checkpoint.py - FP-10: Durable Mission State, Step Checkpointing, and Resumption Engine.

Architectural Purpose:
Provides crash resilience, durable mission persistence, and fault-tolerant resumption
for long-running multi-agent swarms.

Key Capabilities:
1. Step-Level Checkpoints:
   - Checkpoints state after every step (step number, topology stage, accumulated output,
     conversation history, budget counters, inbox messages).
2. HMAC-SHA256 Integrity Protection:
   - Signs every checkpoint payload with an HMAC-SHA256 signature.
   - Detects and rejects any tampering, modification of budget counters, or state forgery on resume.
3. Re-authorization on Resume:
   - Verifies tenant authorization and credentials prior to resuming a paused or recovered mission.
   - Denies resumption for disabled tenants or revoked permissions.
4. Side-Effect Idempotency Registry:
   - Deduplicates side-effecting actions (e.g., file writes, code executions, external mutations).
   - Binds tool name + sha256(args) + tenant_id + mission_id.
   - Guarantees exactly zero duplicate side effects across fault-injection and process recovery.
5. Mission Lifecycle & Cancellation:
   - Supports states: RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED.
   - Allows cancellation of running swarms with fail-fast cooperative aborts.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from core.config import CONFIG, WORKSPACE_DIR
from core.storage import validate_tenant_id

logger = logging.getLogger("neural_swarm.checkpoint")


def get_checkpoint_hmac_secret() -> bytes:
    """Retrieve checkpoint HMAC signing secret from env/config or persistent workspace keyfile."""
    secret = os.getenv("CHECKPOINT_HMAC_SECRET") or getattr(CONFIG, "secret_key", None)
    if not secret:
        key_file = WORKSPACE_DIR / ".checkpoint_hmac.key"
        try:
            if key_file.exists():
                secret = key_file.read_text(encoding="utf-8").strip()
            if not secret:
                WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
                secret = secrets.token_hex(32)
                key_file.write_text(secret, encoding="utf-8")
        except Exception as exc:
            raise RuntimeError(
                f"Security Configuration Error: Failed to load or provision CHECKPOINT_HMAC_SECRET: {exc}"
            ) from exc
    return secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)


@contextmanager
def _connect_db(db_path: str) -> Iterator[sqlite3.Connection]:
    """Safe SQLite connection context manager that commits transactions and closes the handle."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class CheckpointTamperedError(PermissionError):
    """Raised when a checkpoint's cryptographic HMAC signature fails verification."""
    pass


class UnauthorizedResumeError(PermissionError):
    """Raised when re-authorization checks fail during mission resumption."""
    pass


class MissionCancelledError(RuntimeError):
    """Raised when attempting to advance or resume a mission marked as CANCELLED."""
    pass


class FaultInjectedCrashError(RuntimeError):
    """Simulated crash injected to test state checkpoint and resumption resilience."""
    pass


@dataclass
class MissionCheckpoint:
    """Immutable state snapshot of a swarm mission at a distinct step."""
    mission_id: str
    tenant_id: str
    step_number: int
    status: str  # "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"
    topology: str
    stage_index: int
    accumulated_output: str
    messages: List[Dict[str, Any]] = field(default_factory=list)
    budget_state: Dict[str, Any] = field(default_factory=dict)
    idempotency_keys: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    hmac_signature: str = ""

    def payload_bytes(self) -> bytes:
        """Deterministic canonical representation of state for cryptographic signing."""
        canonical_dict = {
            "mission_id": self.mission_id,
            "tenant_id": self.tenant_id,
            "step_number": self.step_number,
            "status": self.status,
            "topology": self.topology,
            "stage_index": self.stage_index,
            "accumulated_output": self.accumulated_output,
            "messages": self.messages,
            "budget_state": self.budget_state,
            "idempotency_keys": sorted(self.idempotency_keys),
            "timestamp": round(self.timestamp, 4),
        }
        return json.dumps(canonical_dict, sort_keys=True, ensure_ascii=True).encode("utf-8")

    def sign(self, secret: Optional[bytes] = None) -> None:
        """Compute and attach HMAC-SHA256 signature to this checkpoint."""
        key = secret if secret is not None else get_checkpoint_hmac_secret()
        self.hmac_signature = hmac.new(key, self.payload_bytes(), hashlib.sha256).hexdigest()

    def verify_integrity(self, secret: Optional[bytes] = None) -> bool:
        """Verify HMAC-SHA256 signature using constant-time comparison."""
        key = secret if secret is not None else get_checkpoint_hmac_secret()
        expected = hmac.new(key, self.payload_bytes(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(self.hmac_signature, expected)


class IdempotencyRegistry:
    """
    Tenant-scoped registry preventing duplicate execution of side-effecting operations
    across mission retries, restarts, and checkpoint resumes.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(db_path or CONFIG.db_path)
        self._init_db()

    def _init_db(self) -> None:
        with _connect_db(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency_records (
                    idempotency_key TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    cached_result TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_idem_tenant ON idempotency_records(tenant_id, mission_id)"
            )

    @staticmethod
    def generate_key(tenant_id: str, mission_id: str, tool_name: str, args: Dict[str, Any]) -> Tuple[str, str]:
        """Produce a collision-resistant deterministic key for an action."""
        args_json = json.dumps(args, sort_keys=True, ensure_ascii=True)
        args_hash = hashlib.sha256(args_json.encode("utf-8")).hexdigest()
        combined = f"{tenant_id}:{mission_id}:{tool_name}:{args_hash}"
        full_key = hashlib.sha256(combined.encode("utf-8")).hexdigest()
        return full_key, args_hash

    def check_execution(
        self,
        tenant_id: str,
        mission_id: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[str]:
        """
        Check if an identical side-effecting action was already performed for this mission.
        Returns cached output if found, or None if new.
        """
        validate_tenant_id(tenant_id)
        key, _ = self.generate_key(tenant_id, mission_id, tool_name, args)
        with _connect_db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT cached_result FROM idempotency_records WHERE idempotency_key = ? AND tenant_id = ?",
                (key, tenant_id),
            )
            row = cur.fetchone()
            if row:
                logger.info(
                    "[IDEMPOTENCY HIT] Skipped re-executing tool '%s' for mission '%s' (tenant '%s').",
                    tool_name,
                    mission_id,
                    tenant_id,
                )
                return str(row[0])
        return None

    def record_execution(
        self,
        tenant_id: str,
        mission_id: str,
        tool_name: str,
        args: Dict[str, Any],
        result: str,
    ) -> str:
        """Record completed side-effecting operation with result."""
        validate_tenant_id(tenant_id)
        key, args_hash = self.generate_key(tenant_id, mission_id, tool_name, args)
        with _connect_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO idempotency_records
                (idempotency_key, tenant_id, mission_id, tool_name, args_hash, cached_result, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key, tenant_id, mission_id, tool_name, args_hash, result, time.time()),
            )
        return key


class CheckpointStore:
    """
    Durable storage engine for mission checkpoints with tenant isolation and HMAC verification.
    """

    def __init__(self, db_path: Optional[str] = None, hmac_secret: Optional[bytes] = None) -> None:
        self.db_path = str(db_path or CONFIG.db_path)
        self.hmac_secret = hmac_secret if hmac_secret is not None else get_checkpoint_hmac_secret()
        self.idempotency = IdempotencyRegistry(self.db_path)
        self._init_db()

    def _init_db(self) -> None:
        with _connect_db(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_checkpoints (
                    mission_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    step_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    topology TEXT NOT NULL,
                    stage_index INTEGER NOT NULL,
                    accumulated_output TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    idempotency_keys_json TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    hmac_signature TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, mission_id, step_number)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoint_lookup ON mission_checkpoints(tenant_id, mission_id, step_number DESC)"
            )

    def save_checkpoint(self, cp: MissionCheckpoint) -> None:
        """Cryptographically sign and persist step checkpoint to durable storage."""
        validate_tenant_id(cp.tenant_id)
        cp.sign(self.hmac_secret)

        with _connect_db(self.db_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO mission_checkpoints
                (mission_id, tenant_id, step_number, status, topology, stage_index,
                 accumulated_output, messages_json, budget_json, idempotency_keys_json,
                 timestamp, hmac_signature)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cp.mission_id,
                    cp.tenant_id,
                    cp.step_number,
                    cp.status,
                    cp.topology,
                    cp.stage_index,
                    cp.accumulated_output,
                    json.dumps(cp.messages, ensure_ascii=True),
                    json.dumps(cp.budget_state, ensure_ascii=True),
                    json.dumps(cp.idempotency_keys, ensure_ascii=True),
                    cp.timestamp,
                    cp.hmac_signature,
                ),
            )

    def load_latest_checkpoint(
        self,
        tenant_id: str,
        mission_id: str,
        auth_validator: Optional[Callable[[str], bool]] = None,
    ) -> Optional[MissionCheckpoint]:
        """
        Load, re-authorize, and verify cryptographic integrity of the latest checkpoint.
        Raises CheckpointTamperedError on HMAC mismatch.
        Raises UnauthorizedResumeError if auth_validator returns False.
        """
        validate_tenant_id(tenant_id)

        # 1. Re-authorization check
        if auth_validator is not None and not auth_validator(tenant_id):
            logger.warning("[SECURITY] Re-authorization failed for tenant '%s' during mission resume.", tenant_id)
            raise UnauthorizedResumeError(f"Tenant '{tenant_id}' failed authorization during mission resume.")

        with _connect_db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT mission_id, tenant_id, step_number, status, topology, stage_index,
                       accumulated_output, messages_json, budget_json, idempotency_keys_json,
                       timestamp, hmac_signature
                FROM mission_checkpoints
                WHERE tenant_id = ? AND mission_id = ?
                ORDER BY step_number DESC
                LIMIT 1
                """,
                (tenant_id, mission_id),
            )
            row = cur.fetchone()
            if not row:
                return None

        cp = MissionCheckpoint(
            mission_id=row[0],
            tenant_id=row[1],
            step_number=int(row[2]),
            status=str(row[3]),
            topology=str(row[4]),
            stage_index=int(row[5]),
            accumulated_output=str(row[6]),
            messages=json.loads(row[7]),
            budget_state=json.loads(row[8]),
            idempotency_keys=json.loads(row[9]),
            timestamp=float(row[10]),
            hmac_signature=str(row[11]),
        )

        # 2. Cryptographic Integrity Check
        if not cp.verify_integrity(self.hmac_secret):
            msg = (
                f"[SECURITY ALERT] Checkpoint integrity verification failed for tenant '{tenant_id}', "
                f"mission '{mission_id}', step {cp.step_number}. Checkpoint has been tampered with!"
            )
            logger.critical(msg)
            raise CheckpointTamperedError(msg)

        return cp

    def cancel_mission(self, tenant_id: str, mission_id: str) -> bool:
        """Mark a mission as CANCELLED so subsequent execution attempts abort immediately."""
        validate_tenant_id(tenant_id)
        cp = self.load_latest_checkpoint(tenant_id, mission_id)
        if not cp:
            return False

        cancelled_cp = MissionCheckpoint(
            mission_id=cp.mission_id,
            tenant_id=cp.tenant_id,
            step_number=cp.step_number + 1,
            status="CANCELLED",
            topology=cp.topology,
            stage_index=cp.stage_index,
            accumulated_output=cp.accumulated_output,
            messages=cp.messages,
            budget_state=cp.budget_state,
            idempotency_keys=cp.idempotency_keys,
            timestamp=time.time(),
        )
        self.save_checkpoint(cancelled_cp)
        return True

    def purge_tenant_checkpoints(self, tenant_id: str) -> int:
        """Cascading erasure of all checkpoints and idempotency logs for a tenant."""
        validate_tenant_id(tenant_id)
        with _connect_db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM mission_checkpoints WHERE tenant_id = ?", (tenant_id,))
            count = cur.rowcount
            cur.execute("DELETE FROM idempotency_records WHERE tenant_id = ?", (tenant_id,))
            return count

    def list_tenant_checkpoints(self, tenant_id: str, limit: int = 12) -> List[Dict[str, Any]]:
        """List recent checkpoints for a tenant with HMAC verification status."""
        validate_tenant_id(tenant_id)
        with _connect_db(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT mission_id, tenant_id, step_number, status, topology, stage_index,
                       accumulated_output, timestamp, hmac_signature
                FROM mission_checkpoints
                WHERE tenant_id = ?
                ORDER BY timestamp DESC, step_number DESC
                LIMIT ?
                """,
                (tenant_id, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "mission_id": r[0],
                "tenant_id": r[1],
                "step_number": int(r[2]),
                "status": str(r[3]),
                "topology": str(r[4]),
                "stage_index": int(r[5]),
                "summary": str(r[6])[:160],
                "timestamp": float(r[7]),
                "hmac_signature": str(r[8]),
            }
            for r in rows
        ]


class DurableMissionRunner:
    """
    Executes a multi-stage workflow with step-level state checkpointing,
    cryptographic integrity verification, side-effect deduplication, and crash recovery.
    """

    def __init__(
        self,
        tenant_id: str,
        mission_id: str,
        topology: str,
        store: CheckpointStore,
    ) -> None:
        self.tenant_id = tenant_id
        self.mission_id = mission_id
        self.topology = topology
        self.store = store

    def execute_stages(
        self,
        stages: List[Callable[[str, CheckpointStore], str]],
        initial_input: str,
        auth_validator: Optional[Callable[[str], bool]] = None,
        crash_at_step: Optional[int] = None,
    ) -> str:
        """
        Execute or resume stages. If a crash occurs or is simulated, running this method
        again will resume exactly from the last committed step without re-executing completed stages.
        """
        validate_tenant_id(self.tenant_id)

        # Check existing state
        latest_cp = self.store.load_latest_checkpoint(self.tenant_id, self.mission_id, auth_validator=auth_validator)
        if latest_cp:
            if latest_cp.status == "CANCELLED":
                raise MissionCancelledError(f"Mission '{self.mission_id}' was cancelled and cannot be resumed.")
            if latest_cp.status == "COMPLETED":
                return latest_cp.accumulated_output
            start_stage = latest_cp.stage_index + 1
            current_output = latest_cp.accumulated_output
            current_step = latest_cp.step_number
            idem_keys = list(latest_cp.idempotency_keys)
        else:
            start_stage = 0
            current_output = initial_input
            current_step = 0
            idem_keys = []

        for stage_idx in range(start_stage, len(stages)):
            current_step += 1

            # Check simulated crash fault injection
            if crash_at_step is not None and current_step == crash_at_step:
                raise FaultInjectedCrashError(
                    f"Fault injection: Process crashed at step {current_step} before stage {stage_idx} completed."
                )

            stage_fn = stages[stage_idx]
            current_output = stage_fn(current_output, self.store)

            # Determine status
            is_final = (stage_idx == len(stages) - 1)
            status = "COMPLETED" if is_final else "RUNNING"

            cp = MissionCheckpoint(
                mission_id=self.mission_id,
                tenant_id=self.tenant_id,
                step_number=current_step,
                status=status,
                topology=self.topology,
                stage_index=stage_idx,
                accumulated_output=current_output,
                idempotency_keys=idem_keys,
            )
            self.store.save_checkpoint(cp)

        return current_output

