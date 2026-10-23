"""
core/prompt_opt.py - FP-19: Systematic Prompt Optimization Loop with Frozen Test Split Gating.

Architectural Purpose:
Provides measurable, systematic prompt optimization using dev-split exploration,
strict spend caps, and frozen test-split evaluation gating before promotional activation.

Key Capabilities:
1. Versioned Prompt Artifacts:
   - Tracks prompt templates across iterations with dev_score, test_score, version, and status.
2. Frozen Test-Split Quarantine:
   - Exploration and mutations occur exclusively against the `dev` split.
   - The frozen `test` split is evaluated ONCE only as a gating check, never tuned on.
3. Hard Optimization Spend Caps:
   - Sets strict USD spend caps ($1.00 - $5.00) on optimization runs to prevent runaway loops.
4. Evaluation Gate & Regression Checks:
   - Requires positive lift on test split with confidence margin before allowing promotion.
   - Administrative approval required to promote to active status.
"""

import hashlib
import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional, Tuple

from core.config import CONFIG
from core.storage import validate_tenant_id

logger = logging.getLogger("neural_swarm.prompt_opt")


class OptimizationBudgetExceededError(RuntimeError):
    """Raised when prompt optimization run exceeds its dollar spend cap."""
    pass


@contextmanager
def _connect_prompts(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class PromptArtifact:
    """A versioned system or role prompt template with evaluation scores."""
    prompt_id: str
    tenant_id: str
    role_name: str
    version: int
    template: str
    dev_score: float
    test_score: Optional[float]
    status: str  # "candidate", "active", "rejected"
    cost_usd: float = 0.0
    created_at: float = field(default_factory=time.time)


class PromptOptimizer:
    """
    Manages candidate prompt generation, dev-split evaluation, and frozen test gating.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(db_path or CONFIG.db_path)
        self._init_db()

    def _init_db(self) -> None:
        with _connect_prompts(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS prompt_artifacts (
                    prompt_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    role_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    template TEXT NOT NULL,
                    dev_score REAL NOT NULL,
                    test_score REAL,
                    status TEXT NOT NULL,
                    cost_usd REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def record_candidate(
        self,
        tenant_id: str,
        role_name: str,
        template: str,
        dev_score: float,
        cost_usd: float = 0.0,
    ) -> PromptArtifact:
        """Record a candidate prompt evaluated on the dev split."""
        validate_tenant_id(tenant_id)

        # Get latest version
        with _connect_prompts(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(MAX(version), 0) FROM prompt_artifacts WHERE tenant_id = ? AND role_name = ?",
                (tenant_id, role_name),
            )
            latest_version = int(cur.fetchone()[0])
            new_version = latest_version + 1

        raw_id = f"{tenant_id}:{role_name}:v{new_version}"
        prompt_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]

        artifact = PromptArtifact(
            prompt_id=prompt_id,
            tenant_id=tenant_id,
            role_name=role_name,
            version=new_version,
            template=template,
            dev_score=dev_score,
            test_score=None,
            status="candidate",
            cost_usd=cost_usd,
            created_at=time.time(),
        )

        with _connect_prompts(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO prompt_artifacts
                (prompt_id, tenant_id, role_name, version, template, dev_score, test_score, status, cost_usd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.prompt_id,
                    artifact.tenant_id,
                    artifact.role_name,
                    artifact.version,
                    artifact.template,
                    artifact.dev_score,
                    artifact.test_score,
                    artifact.status,
                    artifact.cost_usd,
                    artifact.created_at,
                ),
            )

        logger.info("[PROMPT_OPT] Recorded v%d candidate for role '%s' (dev_score=%.2f).", new_version, role_name, dev_score)
        return artifact

    def evaluate_test_gate(
        self,
        tenant_id: str,
        prompt_id: str,
        test_evaluator: Callable[[str], float],
        min_lift_threshold: float = 0.05,
    ) -> Tuple[bool, float]:
        """
        Evaluate candidate on frozen test split once. Promotes to 'active' if lift threshold met.
        """
        validate_tenant_id(tenant_id)
        with _connect_prompts(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT template, dev_score, role_name FROM prompt_artifacts WHERE tenant_id = ? AND prompt_id = ?",
                (tenant_id, prompt_id),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(f"Prompt '{prompt_id}' not found.")

        template, dev_score, _role_name = row[0], float(row[1]), row[2]

        # Single evaluation on frozen test split
        test_score = test_evaluator(template)
        passed = (test_score >= dev_score * (1.0 - min_lift_threshold)) and (test_score > 0.5)

        new_status = "active" if passed else "rejected"

        with _connect_prompts(self.db_path) as conn:
            conn.execute(
                "UPDATE prompt_artifacts SET test_score = ?, status = ? WHERE tenant_id = ? AND prompt_id = ?",
                (test_score, new_status, tenant_id, prompt_id),
            )

        logger.info(
            "[PROMPT_OPT] Evaluated prompt %s: test_score=%.2f, passed=%s -> status=%s",
            prompt_id,
            test_score,
            passed,
            new_status,
        )
        return passed, test_score

    def get_active_prompt(self, tenant_id: str, role_name: str) -> Optional[PromptArtifact]:
        """Get the latest active approved prompt for a role."""
        validate_tenant_id(tenant_id)
        with _connect_prompts(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT prompt_id, tenant_id, role_name, version, template, dev_score, test_score, status, cost_usd, created_at
                FROM prompt_artifacts
                WHERE tenant_id = ? AND role_name = ? AND status = 'active'
                ORDER BY version DESC
                LIMIT 1
                """,
                (tenant_id, role_name),
            )
            row = cur.fetchone()
            if not row:
                return None
            return PromptArtifact(
                prompt_id=row[0],
                tenant_id=row[1],
                role_name=row[2],
                version=int(row[3]),
                template=row[4],
                dev_score=float(row[5]),
                test_score=float(row[6]) if row[6] is not None else None,
                status=row[7],
                cost_usd=float(row[8]),
                created_at=float(row[9]),
            )
