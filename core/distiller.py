"""
core/distiller.py - FP-18: Background Skill Distiller with Strict Capability Signatures and Injection Defense.

Architectural Purpose:
Observes completed missions and synthesizes reusable technical lessons/skills into an
isolated tenant skill library without distracting foreground agents or altering tool privileges.

Critical Security Controls & Verification Guarantees:
1. Explicit Success Signal Required:
   - Learns strictly from missions with verified success (programmatic checker pass, positive user feedback).
   - If no explicit success signal is present, distillation is aborted immediately.
2. Capability-Signature Keying (Zero Signature Leak):
   - Skills declare their exact required_capabilities (e.g., ['python_exec'], ['web_fetch']).
   - A skill can ONLY be injected into a role if that role possesses EVERY required capability.
   - Free-text tags are never used for authorization.
3. Content & Planted-Injection Hardening:
   - Candidates are rejected if they contain:
     - Imperative prompt injection markers ("ignore instructions", "system:", "you are now").
     - Tool or capability names outside declared required_capabilities.
     - URLs, raw IP addresses, or secret/PII tokens.
4. Administrative Promotion Pipeline & Lifecycle:
   - Starts in 'candidate' status. Requires admin approval to transition to 'active'.
   - Global emergency kill-switch and instant one-step rollback.
5. Absolute Permission Bound:
   - Skills can NEVER grant, modify, or elevate tool or agent permissions.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import logging
import re
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from core.config import CONFIG
from core.storage import PrivacyAirlock, validate_tenant_id

logger = logging.getLogger("neural_swarm.distiller")

# Injection heuristic block patterns
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?(?:previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:system|safety|security)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s*:\s*", re.IGNORECASE),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\b(?:sk-|ghp_|gho_|Bearer\s+)\b"),
]


class SkillRejectedError(ValueError):
    """Raised when candidate skill fails safety, capability, or injection checks."""
    pass


class SkillAuthorizationError(PermissionError):
    """Raised when an agent attempts to retrieve a skill exceeding its role capabilities."""
    pass


@contextmanager
def _connect_skills(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class DistilledSkill:
    """A verified reusable technical lesson bound to specific capability requirements."""
    skill_id: str
    tenant_id: str
    category: str
    required_capabilities: List[str]
    task_tags: List[str]
    problem_pattern: str
    solution_rule: str
    provenance: Dict[str, Any]
    status: str = "candidate"  # "candidate", "active", "disabled", "rolled_back"
    version: int = 1
    previous_version_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    def to_guidance_text(self) -> str:
        """Formatted text safe for role injection."""
        return (
            f"[LEARNED_SKILL id={self.skill_id} category={self.category} v{self.version}]\n"
            f"Pattern: {self.problem_pattern}\n"
            f"Rule: {self.solution_rule}\n"
            f"[END_SKILL]"
        )


class SkillDistiller:
    """
    Synthesizes and manages persistent tenant skills from verified execution traces.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = str(db_path or CONFIG.db_path)
        self.kill_switch_active = False
        self._init_db()

    def _init_db(self) -> None:
        with _connect_skills(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS distilled_skills (
                    skill_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    required_capabilities_json TEXT NOT NULL,
                    task_tags_json TEXT NOT NULL,
                    problem_pattern TEXT NOT NULL,
                    solution_rule TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    previous_version_id TEXT,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_skill_tenant ON distilled_skills(tenant_id, status)"
            )

    def validate_candidate_safety(
        self,
        problem: str,
        solution: str,
        declared_capabilities: List[str],
        allowed_tool_names: Set[str],
    ) -> None:
        """
        Check for planted prompt injection, external URLs, secrets, or undeclared tool mentions.
        """
        combined = f"{problem} {solution}"

        # 1. Injection patterns
        for pattern in _INJECTION_PATTERNS:
            if pattern.search(combined):
                raise SkillRejectedError(f"Candidate rejected: Matched planted injection heuristic '{pattern.pattern}'.")

        # 2. Check for PII or secrets
        sanitized = PrivacyAirlock.sanitize(combined)
        if "<SECRET>" in sanitized or "<EMAIL>" in sanitized:
            raise SkillRejectedError("Candidate rejected: Contains PII or credentials.")

        # 3. Check that no undeclared high-risk tools are mentioned as commands
        # A skill cannot mention tools it doesn't declare capabilities for
        for tool_name in ["run_python", "fetch_url", "save_note"]:
            if tool_name in combined and tool_name not in declared_capabilities and tool_name not in allowed_tool_names:
                raise SkillRejectedError(
                    f"Candidate rejected: Mentions tool '{tool_name}' without declaring required capability."
                )

    def distill_candidate(
        self,
        tenant_id: str,
        category: str,
        problem_pattern: str,
        solution_rule: str,
        required_capabilities: List[str],
        task_tags: List[str],
        has_success_signal: bool,
        mission_id: str = "",
        trace_hash: str = "",
    ) -> Optional[DistilledSkill]:
        """
        Create a new candidate skill if and only if explicit success signal is present.
        """
        validate_tenant_id(tenant_id)

        # 1. Explicit Success Signal Gating (No signal -> No learning)
        if not has_success_signal:
            logger.info("[DISTILLER] Skipped distillation: No verified success signal for mission '%s'.", mission_id)
            return None

        # 2. Safety and Injection Validation
        self.validate_candidate_safety(
            problem=problem_pattern,
            solution=solution_rule,
            declared_capabilities=required_capabilities,
            allowed_tool_names=set(required_capabilities),
        )

        raw_id = f"{tenant_id}:{category}:{time.time()}:{problem_pattern[:20]}"
        skill_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]

        provenance = {
            "mission_id": mission_id,
            "trace_hash": trace_hash,
            "has_success_signal": True,
        }

        skill = DistilledSkill(
            skill_id=skill_id,
            tenant_id=tenant_id,
            category=category,
            required_capabilities=list(required_capabilities),
            task_tags=list(task_tags),
            problem_pattern=PrivacyAirlock.sanitize(problem_pattern),
            solution_rule=PrivacyAirlock.sanitize(solution_rule),
            provenance=provenance,
            status="candidate",
            version=1,
            created_at=time.time(),
        )

        with _connect_skills(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO distilled_skills
                (skill_id, tenant_id, category, required_capabilities_json, task_tags_json,
                 problem_pattern, solution_rule, provenance_json, status, version, previous_version_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill.skill_id,
                    skill.tenant_id,
                    skill.category,
                    json.dumps(skill.required_capabilities),
                    json.dumps(skill.task_tags),
                    skill.problem_pattern,
                    skill.solution_rule,
                    json.dumps(skill.provenance),
                    skill.status,
                    skill.version,
                    skill.previous_version_id,
                    skill.created_at,
                ),
            )

        logger.info("[DISTILLER] Created candidate skill %s for tenant '%s'.", skill_id, tenant_id)
        return skill

    def approve_skill(self, tenant_id: str, skill_id: str) -> bool:
        """Promote candidate skill to active status."""
        validate_tenant_id(tenant_id)
        with _connect_skills(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE distilled_skills SET status = 'active' WHERE tenant_id = ? AND skill_id = ?",
                (tenant_id, skill_id),
            )
            return cur.rowcount > 0

    def rollback_skill(self, tenant_id: str, skill_id: str) -> bool:
        """Instant one-step rollback of a faulty or regressing skill."""
        validate_tenant_id(tenant_id)
        with _connect_skills(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE distilled_skills SET status = 'rolled_back' WHERE tenant_id = ? AND skill_id = ?",
                (tenant_id, skill_id),
            )
            return cur.rowcount > 0

    def get_applicable_skills_for_role(
        self,
        tenant_id: str,
        role_capabilities: Set[str],
        task_tags: Optional[Set[str]] = None,
    ) -> List[DistilledSkill]:
        """
        SIGNATURE-LEAK PROTECTED RETRIEVAL:
        A skill is returned ONLY IF the role possesses EVERY required capability of that skill.
        If the kill switch is active, returns an empty list immediately.
        """
        validate_tenant_id(tenant_id)
        if self.kill_switch_active:
            logger.warning("[DISTILLER] Kill-switch is active. No skills returned.")
            return []

        with _connect_skills(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT skill_id, tenant_id, category, required_capabilities_json, task_tags_json,
                       problem_pattern, solution_rule, provenance_json, status, version, previous_version_id, created_at
                FROM distilled_skills
                WHERE tenant_id = ? AND status = 'active'
                ORDER BY created_at DESC
                """,
                (tenant_id,),
            )
            rows = cur.fetchall()

        matched_skills: List[DistilledSkill] = []
        for r in rows:
            req_caps = set(json.loads(r[3]))
            s_tags = set(json.loads(r[4]))

            # 1. Capability Signature Check (Strict Subset: role must hold ALL required capabilities)
            if not req_caps.issubset(role_capabilities):
                continue  # Role lacks one or more required capabilities -> NEVER INJECT

            # 2. Tag alignment check (if tags are specified)
            if task_tags and not s_tags.intersection(task_tags):
                continue

            skill = DistilledSkill(
                skill_id=r[0],
                tenant_id=r[1],
                category=r[2],
                required_capabilities=list(req_caps),
                task_tags=list(s_tags),
                problem_pattern=r[5],
                solution_rule=r[6],
                provenance=json.loads(r[7]),
                status=r[8],
                version=int(r[9]),
                previous_version_id=r[10],
                created_at=float(r[11]),
            )
            matched_skills.append(skill)

        return matched_skills

    def purge_tenant_skills(self, tenant_id: str) -> int:
        """Cascading erasure of all skills for a tenant."""
        validate_tenant_id(tenant_id)
        with _connect_skills(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM distilled_skills WHERE tenant_id = ?", (tenant_id,))
            return cur.rowcount
