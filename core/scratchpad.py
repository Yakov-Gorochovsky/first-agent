"""
core/scratchpad.py - FP-07: Call-by-Reference Scratchpad & Out-of-Band Memory Store.

Architectural Purpose:
Passes bulky tool outputs and data frames as compact reference tokens instead of
inlining thousands of raw tokens into agent context windows and inter-agent handoffs.

Key Security & Performance Capabilities:
1. 128-Bit Cryptographic Reference Tokens:
   - References are generated with secrets.token_hex(16) (128-bit cryptographic entropy).
   - Zero Filesystem Path Mapping: IDs are stored directly in SQLite BLOB tables and
     never translated into filesystem paths, eliminating directory traversal vectors.
2. Strict Multi-Tenant Scoping:
   - Every dereference strictly verifies tenant ownership. Cross-tenant queries and
     guessed IDs fail closed with uniform access denials (preventing oracle probing).
3. Taint Inheritance (Zero Injection Laundering):
   - Items stored with untrusted provenance retain their trust label upon dereference.
   - Pointers cannot be used to bypass FP-02 TaintTracker boundaries.
4. Deterministic Structural Summaries:
   - Generates deterministic previews (byte size, line count, sha256 digest, head preview)
     so downstream agents can deliberate without immediate costly dereferencing.
5. Quota & Expiration Controls:
   - Enforces per-payload byte limits, tenant storage quotas, and TTL expiration.
"""

import hashlib
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

from core.config import CONFIG
from core.storage import validate_tenant_id

logger = logging.getLogger("neural_swarm.scratchpad")


class ScratchpadAccessDeniedError(PermissionError):
    """Raised when an ID is invalid, unowned by the requesting tenant, or does not exist."""
    pass


class ScratchpadExpiredError(KeyError):
    """Raised when dereferencing an item whose TTL has elapsed."""
    pass


class ScratchpadQuotaExceededError(ValueError):
    """Raised when a payload exceeds max size or tenant exceeds quota."""
    pass


class ScratchpadIntegrityError(PermissionError):
    """Raised when the content hash does not match stored content."""
    pass


@contextmanager
def _connect_scratchpad(db_path: str) -> Iterator[sqlite3.Connection]:
    """Safe SQLite connection context manager committing and closing handles."""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass(frozen=True)
class ScratchpadPointer:
    """Opaque reference token passed inside agent contexts and message handoffs."""
    ref_id: str
    tenant_id: str
    content_hash: str
    size_bytes: int
    trust_label: str
    summary: str
    created_at: float
    expires_at: float

    def to_reference_tag(self) -> str:
        """Formatted string representation safe for insertion into prompt context."""
        return (
            f"[SCRATCHPAD_REF id={self.ref_id} bytes={self.size_bytes} "
            f"hash={self.content_hash[:8]} trust={self.trust_label}]\n"
            f"Summary: {self.summary}"
        )


@dataclass
class ScratchpadItem:
    """Retrieved full payload with provenance and security metadata."""
    ref_id: str
    tenant_id: str
    content: str
    content_hash: str
    size_bytes: int
    trust_label: str
    summary: str
    created_at: float
    expires_at: float


class ScratchpadStore:
    """
    Tenant-isolated storage engine managing out-of-band bulky data payloads.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        default_ttl_seconds: int = 86400,  # 24 hours
        max_item_bytes: int = 10 * 1024 * 1024,  # 10 MB
        tenant_quota_bytes: int = 50 * 1024 * 1024,  # 50 MB
    ) -> None:
        self.db_path = str(db_path or CONFIG.db_path)
        self.default_ttl_seconds = default_ttl_seconds
        self.max_item_bytes = max_item_bytes
        self.tenant_quota_bytes = tenant_quota_bytes
        self._init_db()

    def _init_db(self) -> None:
        with _connect_scratchpad(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scratchpad_blobs (
                    ref_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    trust_label TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scratch_lookup ON scratchpad_blobs(tenant_id, ref_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scratch_expiry ON scratchpad_blobs(expires_at)"
            )

    @staticmethod
    def generate_deterministic_summary(content: str, max_preview: int = 120) -> str:
        """Generate deterministic metadata preview without calling LLM inference."""
        lines = content.splitlines()
        line_count = len(lines)
        size_bytes = len(content.encode("utf-8"))
        snippet = content[:max_preview].replace("\n", " ").strip()
        if len(content) > max_preview:
            snippet += "..."
        return f"Lines: {line_count}, Bytes: {size_bytes} | Preview: {snippet}"

    def get_tenant_usage(self, tenant_id: str) -> int:
        """Calculate total stored scratchpad bytes for tenant."""
        validate_tenant_id(tenant_id)
        with _connect_scratchpad(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM scratchpad_blobs WHERE tenant_id = ?",
                (tenant_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def store(
        self,
        tenant_id: str,
        content: str,
        trust_label: str = "internal",
        ttl_seconds: Optional[int] = None,
        custom_summary: Optional[str] = None,
    ) -> ScratchpadPointer:
        """
        Store bulky string payload out-of-band and return a 128-bit opaque reference pointer.
        """
        validate_tenant_id(tenant_id)
        content_bytes = content.encode("utf-8")
        size_bytes = len(content_bytes)

        if size_bytes > self.max_item_bytes:
            raise ScratchpadQuotaExceededError(
                f"Payload size ({size_bytes} bytes) exceeds max item size ({self.max_item_bytes} bytes)."
            )

        current_usage = self.get_tenant_usage(tenant_id)
        if current_usage + size_bytes > self.tenant_quota_bytes:
            raise ScratchpadQuotaExceededError(
                f"Tenant '{tenant_id}' exceeded storage quota ({current_usage + size_bytes} > {self.tenant_quota_bytes} bytes)."
            )

        ref_id = secrets.token_hex(16)  # 128-bit cryptographically secure token
        content_hash = hashlib.sha256(content_bytes).hexdigest()
        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds
        expires_at = now + ttl
        summary = custom_summary or self.generate_deterministic_summary(content)

        with _connect_scratchpad(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO scratchpad_blobs
                (ref_id, tenant_id, content, content_hash, size_bytes, trust_label, summary, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ref_id, tenant_id, content, content_hash, size_bytes, trust_label, summary, now, expires_at),
            )

        logger.info(
            "[SCRATCHPAD] Stored payload (%d bytes) for tenant '%s' as ref '%s' (hash: %s).",
            size_bytes,
            tenant_id,
            ref_id,
            content_hash[:8],
        )

        return ScratchpadPointer(
            ref_id=ref_id,
            tenant_id=tenant_id,
            content_hash=content_hash,
            size_bytes=size_bytes,
            trust_label=trust_label,
            summary=summary,
            created_at=now,
            expires_at=expires_at,
        )

    def dereference(self, tenant_id: str, ref_id: str) -> ScratchpadItem:
        """
        Retrieve original content by reference token.
        Enforces tenant authorization, expiration check, and cryptographic content verification.
        """
        validate_tenant_id(tenant_id)

        # Defense against ID-to-path manipulation
        if not ref_id or not isinstance(ref_id, str) or len(ref_id) != 32:
            raise ScratchpadAccessDeniedError("Invalid reference ID format.")

        with _connect_scratchpad(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ref_id, tenant_id, content, content_hash, size_bytes, trust_label, summary, created_at, expires_at
                FROM scratchpad_blobs
                WHERE ref_id = ? AND tenant_id = ?
                """,
                (ref_id, tenant_id),
            )
            row = cur.fetchone()
            if not row:
                logger.warning(
                    "[SECURITY] Denied unauthorized or non-existent scratchpad dereference ref '%s' for tenant '%s'.",
                    ref_id,
                    tenant_id,
                )
                raise ScratchpadAccessDeniedError(f"Reference '{ref_id}' not found or access denied.")

        now = time.time()
        expires_at = float(row[8])
        if now > expires_at:
            logger.info("[SCRATCHPAD] Reference '%s' expired at %f (now %f).", ref_id, expires_at, now)
            raise ScratchpadExpiredError(f"Reference '{ref_id}' has expired.")

        content = str(row[2])
        expected_hash = str(row[3])
        actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        if actual_hash != expected_hash:
            logger.critical("[SCRATCHPAD INTEGRITY ERROR] Content hash mismatch for ref '%s'!", ref_id)
            raise ScratchpadIntegrityError(f"Cryptographic hash mismatch for reference '{ref_id}'.")

        return ScratchpadItem(
            ref_id=str(row[0]),
            tenant_id=str(row[1]),
            content=content,
            content_hash=expected_hash,
            size_bytes=int(row[4]),
            trust_label=str(row[5]),
            summary=str(row[6]),
            created_at=float(row[7]),
            expires_at=expires_at,
        )

    def maybe_pointerize(
        self,
        tenant_id: str,
        content: str,
        threshold_bytes: int = 1024,
        trust_label: str = "internal",
    ) -> Tuple[str, Optional[ScratchpadPointer]]:
        """
        If content size exceeds threshold, store in scratchpad and return formatted pointer tag.
        Otherwise return content unchanged.
        """
        validate_tenant_id(tenant_id)
        size_bytes = len(content.encode("utf-8"))
        if size_bytes > threshold_bytes:
            pointer = self.store(tenant_id, content, trust_label=trust_label)
            return pointer.to_reference_tag(), pointer
        return content, None

    def cleanup_expired(self) -> int:
        """Purge all expired blobs across the system."""
        now = time.time()
        with _connect_scratchpad(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM scratchpad_blobs WHERE expires_at < ?", (now,))
            return cur.rowcount

    def purge_tenant(self, tenant_id: str) -> int:
        """Cascading deletion of all scratchpad blobs for a tenant."""
        validate_tenant_id(tenant_id)
        with _connect_scratchpad(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM scratchpad_blobs WHERE tenant_id = ?", (tenant_id,))
            return cur.rowcount
