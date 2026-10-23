"""
core/storage.py - Swappable Storage Backend + Multi-Tenant Privacy Airlock.

Implements:
1. StorageBackend Protocol (loosely coupled interface; swap SQLite for Postgres anytime).
2. PrivacyAirlock: Scrubs PII, URLs, IPs, emails, keys, and numbers before saving
   procedural lessons to the shared `global_skills` table.
3. SQLiteStorage: Built-in zero-config database with:
   - Strictly isolated `tenant_memories` and `tenant_sessions` per `tenant_id`
   - Shared `global_skills` (procedural playbooks learned across all swarms)
   - Auto-pruning `swarm_traces` table so the `.db` file never bloats.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, Iterator, List, Optional, Protocol, Tuple, Union
from core.config import CONFIG


TENANT_ID_REGEX = r"^[A-Za-z0-9_-]{1,64}$"
_TENANT_ID_PATTERN = re.compile(TENANT_ID_REGEX)


def validate_tenant_id(tenant_id: str) -> str:
    """Strictly validate tenant_id against ^[A-Za-z0-9_-]{1,64}$.

    Rejects non-matching characters (dots, spaces, slashes) to prevent cross-tenant
    collision vulnerabilities (e.g., 'acme.corp' colliding with 'acme_corp').
    Raises:
        ValueError: If tenant_id is not a string matching the strict regex pattern.
    """
    if not isinstance(tenant_id, str) or not _TENANT_ID_PATTERN.match(tenant_id):
        raise ValueError(
            f"Invalid tenant_id {tenant_id!r}. Must match regex {TENANT_ID_REGEX} (1-64 alphanumeric chars, underscores, or hyphens)."
        )
    return tenant_id


# Backwards-compatible alias for existing imports
sanitize_tenant_id = validate_tenant_id


class PrivacyAirlock:
    """Scrubs client-specific PII, secrets, URLs, and numbers from procedural skills."""

    _PATTERNS: List[Tuple[re.Pattern, str]] = [
        # Email addresses
        (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "<EMAIL>"),
        # URLs
        (re.compile(r"https?://[^\s\"'>)]+"), "<URL>"),
        # IPv4 addresses
        (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<IP>"),
        # API keys / tokens
        (re.compile(r"\b(?:sk-|ghp_|gho_|AIza|Bearer\s+)[A-Za-z0-9_\-]{8,}\b"), "<SECRET>"),
        # File paths (Windows & Unix)
        (re.compile(r"(?:[A-Za-z]:\\|/(?:Users|home|var|etc|tmp)/)[^\s\"']+"), "<PATH>"),
        # Long numbers (financials, phone numbers, IDs >= 4 digits)
        (re.compile(r"\b\d{4,}\b"), "<NUM>"),
    ]

    @classmethod
    def sanitize(cls, text: str) -> str:
        cleaned = text.strip()
        for pattern, replacement in cls._PATTERNS:
            cleaned = pattern.sub(replacement, cleaned)
        return cleaned

    @classmethod
    def scrub_text(cls, text: str) -> str:
        return cls.sanitize(text)


class StorageBackend(Protocol):
    """Abstract interface for swarm persistence (SQLite, PostgreSQL, etc.)."""

    def save_tenant_memory(self, tenant_id: str, key: str, fact: str) -> None: ...
    def get_tenant_memories(self, tenant_id: str) -> Dict[str, str]: ...
    def add_global_skill(self, category: str, problem: str, solution: str) -> Dict[str, str]: ...
    def get_global_skills(self, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...
    def append_session_turn(self, tenant_id: str, role: str, content: str) -> None: ...
    def get_session_turns(self, tenant_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]: ...
    def clear_session(self, tenant_id: str) -> None: ...
    def log_trace(self, tenant_id: str, agent_name: str, event_type: str, payload: Dict[str, Any]) -> None: ...
    def save_usage_record(self, record: Any) -> None: ...
    def get_usage_records(self, tenant_id: str, limit: Optional[int] = None) -> List[Dict[str, Any]]: ...


class SQLiteStorage:
    """Zero-cost local SQLite storage with multi-tenant isolation and auto-pruning."""

    def __init__(
        self,
        db_path: Optional[Union[str, Path]] = None,
        max_trace_rows: Optional[int] = None,
        max_session_turns: Optional[int] = None,
    ) -> None:
        raw_path = db_path or CONFIG.db_path
        self.db_path = Path(raw_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_trace_rows = max_trace_rows if max_trace_rows is not None else CONFIG.max_trace_rows
        self.max_session_turns = max_session_turns if max_session_turns is not None else CONFIG.max_session_turns
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenant_memories (
                    tenant_id TEXT NOT NULL,
                    key TEXT NOT NULL,
                    fact TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, key)
                );

                CREATE TABLE IF NOT EXISTS tenant_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS global_skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    problem_pattern TEXT NOT NULL,
                    solution_rule TEXT NOT NULL,
                    learned_count INTEGER NOT NULL DEFAULT 1,
                    updated_at REAL NOT NULL,
                    UNIQUE(category, problem_pattern)
                );

                CREATE TABLE IF NOT EXISTS swarm_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tenant_sessions ON tenant_sessions(tenant_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_swarm_traces_id ON swarm_traces(id DESC);

                CREATE TABLE IF NOT EXISTS usage_ledger (
                    call_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    cached_prompt_tokens INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    cost_usd REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_usage_ledger_tenant ON usage_ledger(tenant_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_ledger_mission ON usage_ledger(mission_id);
                """
            )

    def save_tenant_memory(self, tenant_id: str, key: str, fact: str) -> None:
        safe_tenant = sanitize_tenant_id(tenant_id)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO tenant_memories (tenant_id, key, fact, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, key) DO UPDATE SET
                    fact = excluded.fact,
                    updated_at = excluded.updated_at;
                """,
                (safe_tenant, key.strip()[:120], fact.strip()[:2000], time.time()),
            )

    def get_tenant_memories(self, tenant_id: str) -> Dict[str, str]:
        safe_tenant = sanitize_tenant_id(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT key, fact FROM tenant_memories WHERE tenant_id = ? ORDER BY key",
                (safe_tenant,),
            ).fetchall()
            return {row["key"]: row["fact"] for row in rows}

    def add_global_skill(self, category: str, problem: str, solution: str) -> Dict[str, str]:
        """Pass a learned technical rule through the PrivacyAirlock and store in global_skills."""
        safe_category = PrivacyAirlock.sanitize(category or "general")[:60]
        safe_problem = PrivacyAirlock.sanitize(problem)[:300]
        safe_solution = PrivacyAirlock.sanitize(solution)[:500]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO global_skills (category, problem_pattern, solution_rule, learned_count, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(category, problem_pattern) DO UPDATE SET
                    solution_rule = excluded.solution_rule,
                    learned_count = global_skills.learned_count + 1,
                    updated_at = excluded.updated_at;
                """,
                (safe_category, safe_problem, safe_solution, time.time()),
            )
        return {
            "category": safe_category,
            "problem_pattern": safe_problem,
            "solution_rule": safe_solution,
        }

    def get_global_skills(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        eff_limit = limit if limit is not None else CONFIG.max_global_skills
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT category, problem_pattern, solution_rule, learned_count
                FROM global_skills
                ORDER BY learned_count DESC, updated_at DESC
                LIMIT ?
                """,
                (eff_limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def append_session_turn(self, tenant_id: str, role: str, content: str) -> None:
        safe_tenant = sanitize_tenant_id(tenant_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tenant_sessions (tenant_id, role, content, created_at) VALUES (?, ?, ?, ?)",
                (safe_tenant, role, content, time.time()),
            )
            conn.execute(
                """
                DELETE FROM tenant_sessions
                WHERE tenant_id = ? AND id NOT IN (
                    SELECT id FROM tenant_sessions WHERE tenant_id = ? ORDER BY id DESC LIMIT ?
                )
                """,
                (safe_tenant, safe_tenant, self.max_session_turns),
            )

    def get_session_turns(self, tenant_id: str, limit: Optional[int] = None) -> List[Dict[str, str]]:
        safe_tenant = sanitize_tenant_id(tenant_id)
        eff_limit = limit if limit is not None else CONFIG.context_session_turns
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM tenant_sessions
                    WHERE tenant_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (safe_tenant, eff_limit),
            ).fetchall()
            return [{"role": row["role"], "content": row["content"]} for row in rows]

    def clear_session(self, tenant_id: str) -> None:
        safe_tenant = sanitize_tenant_id(tenant_id)
        with self._connect() as conn:
            conn.execute("DELETE FROM tenant_sessions WHERE tenant_id = ?", (safe_tenant,))

    def log_trace(
        self,
        tenant_id: str,
        agent_name: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        safe_tenant = sanitize_tenant_id(tenant_id)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO swarm_traces (tenant_id, agent_name, event_type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (safe_tenant, agent_name, event_type, json.dumps(payload, default=str), time.time()),
            )
            conn.execute(
                """
                DELETE FROM swarm_traces
                WHERE id NOT IN (
                    SELECT id FROM swarm_traces ORDER BY id DESC LIMIT ?
                )
                """,
                (self.max_trace_rows,),
            )

    def save_usage_record(self, record: Any) -> None:
        """Persist a token/latency/cost ledger record."""
        rec_dict = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        safe_tenant = sanitize_tenant_id(rec_dict["tenant_id"])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO usage_ledger (
                    call_id, mission_id, step_id, tenant_id, agent_name,
                    provider, model, prompt_tokens, completion_tokens,
                    cached_prompt_tokens, latency_ms, cost_usd, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec_dict["call_id"],
                    rec_dict["mission_id"],
                    rec_dict["step_id"],
                    safe_tenant,
                    rec_dict["agent_name"],
                    rec_dict["provider"],
                    rec_dict["model"],
                    int(rec_dict["prompt_tokens"]),
                    int(rec_dict["completion_tokens"]),
                    int(rec_dict.get("cached_prompt_tokens", 0)),
                    float(rec_dict["latency_ms"]),
                    float(rec_dict["cost_usd"]),
                    float(rec_dict.get("created_at", time.time())),
                ),
            )

    def get_usage_records(
        self, tenant_id: str, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Retrieve token and cost ledger records for a tenant."""
        safe_tenant = sanitize_tenant_id(tenant_id)
        eff_limit = limit if limit is not None else 100
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT call_id, mission_id, step_id, tenant_id, agent_name,
                       provider, model, prompt_tokens, completion_tokens,
                       cached_prompt_tokens, latency_ms, cost_usd, created_at
                FROM usage_ledger
                WHERE tenant_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (safe_tenant, eff_limit),
            ).fetchall()
            return [dict(row) for row in rows]
