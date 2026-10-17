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
from typing import Any, Dict, Iterator, List, Optional, Protocol, Tuple
from core.config import CONFIG


def sanitize_tenant_id(tenant_id: str) -> str:
    """Restrict tenant_id to safe alphanumeric, underscore, and hyphen characters."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", (tenant_id or "").strip())[:64]
    return cleaned or "default"


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


class StorageBackend(Protocol):
    """Abstract interface for swarm persistence (SQLite, PostgreSQL, etc.)."""

    def save_tenant_memory(self, tenant_id: str, key: str, fact: str) -> None: ...
    def get_tenant_memories(self, tenant_id: str) -> Dict[str, str]: ...
    def add_global_skill(self, category: str, problem: str, solution: str) -> Dict[str, str]: ...
    def get_global_skills(self, limit: int = 10) -> List[Dict[str, Any]]: ...
    def append_session_turn(self, tenant_id: str, role: str, content: str) -> None: ...
    def get_session_turns(self, tenant_id: str, limit: int = 12) -> List[Dict[str, str]]: ...
    def clear_session(self, tenant_id: str) -> None: ...
    def log_trace(self, tenant_id: str, agent_name: str, event_type: str, payload: Dict[str, Any]) -> None: ...


class SQLiteStorage:
    """Zero-cost local SQLite storage with multi-tenant isolation and auto-pruning."""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        max_trace_rows: Optional[int] = None,
        max_session_turns: Optional[int] = None,
    ) -> None:
        self.db_path = db_path or CONFIG.db_path
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

    def get_global_skills(self, limit: int = CONFIG.max_global_skills) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT category, problem_pattern, solution_rule, learned_count
                FROM global_skills
                ORDER BY learned_count DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
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

    def get_session_turns(self, tenant_id: str, limit: int = CONFIG.context_session_turns) -> List[Dict[str, str]]:
        safe_tenant = sanitize_tenant_id(tenant_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content FROM (
                    SELECT id, role, content FROM tenant_sessions
                    WHERE tenant_id = ? ORDER BY id DESC LIMIT ?
                ) ORDER BY id ASC
                """,
                (safe_tenant, limit),
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
