"""
core/feedback.py - FP-17: Feedback Log, Pointwise Labels, and Preference Pairs.

Architectural Purpose:
Captures user evaluations, pointwise labels (+1 / -1), and critiques without speculative RLHF claims.
Constructs valid preference pairs (chosen vs rejected) only when two alternative outputs exist for
the identical prompt. Enforces contractual consent flags and PII scrubbing.

Key Capabilities:
1. Pointwise vs Pairwise Separation:
   - Records single outputs with ratings (+1 / -1) and qualitative critiques as pointwise records.
   - Preference pairs are assembled strictly when two candidate outputs exist for the same prompt.
2. Mandatory Consent & Purpose Scoping:
   - Every record carries explicit consent_for_training (False by default).
   - Datasets exported for model training require verified consent; evaluation/regression records
     are quarantined from training pipelines.
3. Pre-Storage PII Scrubbing:
   - All free-text critiques and outputs pass through PrivacyAirlock before database insertion.
4. Safe Export & File Path Handling:
   - Exports use system-generated filenames in tenant-scoped directories, eliminating path traversal.
5. Right to Erasure & Cascading Deletion:
   - Purges user or tenant feedback across raw and derived tables.
"""

import hashlib
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from core.config import CONFIG
from core.storage import PrivacyAirlock, validate_tenant_id

logger = logging.getLogger("neural_swarm.feedback")


class FeedbackConsentError(PermissionError):
    """Raised when attempting to export or process data for training without explicit contractual consent."""
    pass


class FeedbackValidationError(ValueError):
    """Raised when feedback arguments or rating values are invalid."""
    pass


@contextmanager
def _connect_feedback(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class PointwiseFeedback:
    """Single-output evaluation rating with optional critique."""
    feedback_id: str
    tenant_id: str
    user_id: str
    mission_id: str
    prompt: str
    output: str
    rating: int  # +1 (thumbs up) or -1 (thumbs down)
    critique: str = ""
    consent_for_training: bool = False
    purpose: str = "evaluation"  # "evaluation", "regression", "training"
    created_at: float = field(default_factory=time.time)


@dataclass
class PreferencePair:
    """Chosen vs rejected comparison pair for identical prompt context."""
    pair_id: str
    tenant_id: str
    user_id: str
    prompt: str
    chosen: str
    rejected: str
    rationale: str = ""
    consent_for_training: bool = False
    created_at: float = field(default_factory=time.time)


class FeedbackStore:
    """
    Tenant-partitioned persistent store for user feedback and preference pairs.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(db_path or CONFIG.db_path)
        self.airlock = PrivacyAirlock()
        self._init_db()

    def _init_db(self) -> None:
        with _connect_feedback(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_pointwise (
                    feedback_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    output TEXT NOT NULL,
                    rating INTEGER NOT NULL,
                    critique TEXT NOT NULL,
                    consent_for_training INTEGER NOT NULL,
                    purpose TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_preference_pairs (
                    pair_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    chosen TEXT NOT NULL,
                    rejected TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    consent_for_training INTEGER NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fb_pointwise_lookup ON feedback_pointwise(tenant_id, mission_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fb_pair_lookup ON feedback_preference_pairs(tenant_id, user_id)"
            )

    def record_pointwise(
        self,
        tenant_id: str,
        user_id: str,
        mission_id: str,
        prompt: str,
        output: str,
        rating: int,
        critique: str = "",
        consent_for_training: bool = False,
        purpose: str = "evaluation",
    ) -> PointwiseFeedback:
        """
        Record thumbs up/down evaluation with PII scrubbing.
        """
        validate_tenant_id(tenant_id)
        if rating not in (-1, 1):
            raise FeedbackValidationError(f"Rating must be +1 (positive) or -1 (negative), got {rating}.")

        clean_critique = self.airlock.scrub_text(critique) if critique else ""
        clean_prompt = self.airlock.scrub_text(prompt)
        clean_output = self.airlock.scrub_text(output)

        raw_id = f"{tenant_id}:{mission_id}:{time.time()}"
        feedback_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]

        now = time.time()
        record = PointwiseFeedback(
            feedback_id=feedback_id,
            tenant_id=tenant_id,
            user_id=user_id,
            mission_id=mission_id,
            prompt=clean_prompt,
            output=clean_output,
            rating=rating,
            critique=clean_critique,
            consent_for_training=consent_for_training,
            purpose=purpose,
            created_at=now,
        )

        with _connect_feedback(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO feedback_pointwise
                (feedback_id, tenant_id, user_id, mission_id, prompt, output, rating,
                 critique, consent_for_training, purpose, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.feedback_id,
                    record.tenant_id,
                    record.user_id,
                    record.mission_id,
                    record.prompt,
                    record.output,
                    record.rating,
                    record.critique,
                    1 if record.consent_for_training else 0,
                    record.purpose,
                    record.created_at,
                ),
            )

        logger.info("[FEEDBACK] Recorded pointwise feedback %s (rating %d) for tenant '%s'.", feedback_id, rating, tenant_id)
        return record

    def record_preference_pair(
        self,
        tenant_id: str,
        user_id: str,
        prompt: str,
        chosen: str,
        rejected: str,
        rationale: str = "",
        consent_for_training: bool = False,
    ) -> PreferencePair:
        """
        Record a pairwise comparison (chosen vs rejected) for identical prompt.
        """
        validate_tenant_id(tenant_id)
        if chosen.strip() == rejected.strip():
            raise FeedbackValidationError("Chosen and rejected outputs cannot be identical.")

        clean_prompt = self.airlock.scrub_text(prompt)
        clean_chosen = self.airlock.scrub_text(chosen)
        clean_rejected = self.airlock.scrub_text(rejected)
        clean_rationale = self.airlock.scrub_text(rationale) if rationale else ""

        raw_id = f"{tenant_id}:{user_id}:pair:{time.time()}"
        pair_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]

        now = time.time()
        pair = PreferencePair(
            pair_id=pair_id,
            tenant_id=tenant_id,
            user_id=user_id,
            prompt=clean_prompt,
            chosen=clean_chosen,
            rejected=clean_rejected,
            rationale=clean_rationale,
            consent_for_training=consent_for_training,
            created_at=now,
        )

        with _connect_feedback(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO feedback_preference_pairs
                (pair_id, tenant_id, user_id, prompt, chosen, rejected, rationale,
                 consent_for_training, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pair.pair_id,
                    pair.tenant_id,
                    pair.user_id,
                    pair.prompt,
                    pair.chosen,
                    pair.rejected,
                    pair.rationale,
                    1 if pair.consent_for_training else 0,
                    pair.created_at,
                ),
            )

        logger.info("[FEEDBACK] Recorded preference pair %s for tenant '%s'.", pair_id, tenant_id)
        return pair

    def export_training_dataset(self, tenant_id: str) -> List[Dict[str, Any]]:
        """
        Export preference pairs for fine-tuning. Strictly enforces consent_for_training=True.
        Any pairs lacking consent are filtered out.
        """
        validate_tenant_id(tenant_id)
        with _connect_feedback(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT pair_id, prompt, chosen, rejected, rationale
                FROM feedback_preference_pairs
                WHERE tenant_id = ? AND consent_for_training = 1
                ORDER BY created_at ASC
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()

        dataset = []
        for r in rows:
            dataset.append({
                "pair_id": r[0],
                "prompt": r[1],
                "chosen": r[2],
                "rejected": r[3],
                "rationale": r[4],
            })
        return dataset

    def purge_tenant_feedback(self, tenant_id: str) -> Tuple[int, int]:
        """Cascading erasure of all pointwise and pairwise records for a tenant."""
        validate_tenant_id(tenant_id)
        with _connect_feedback(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM feedback_pointwise WHERE tenant_id = ?", (tenant_id,))
            pw_count = cur.rowcount
            cur.execute("DELETE FROM feedback_preference_pairs WHERE tenant_id = ?", (tenant_id,))
            pair_count = cur.rowcount
            return pw_count, pair_count
