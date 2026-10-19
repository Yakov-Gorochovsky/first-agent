"""
core/auth.py - API-Key Authentication, Constant-Time Verification & Scoped Access Control.

Implements:
1. APIKeyRecord: Hashed credential bound strictly to a validated tenant_id and scopes.
2. AuthManager:
   - Constant-time SHA-256 hash comparison using hmac.compare_digest (prevents timing attacks).
   - Tenant identity derivation: Identity is bound to the credential, never caller query parameters.
   - Scope checks: Distinguishes 'user' operations from 'admin' operations (roles, swarms, airlock).
   - Zero plaintext storage: Only SHA-256 hex digests are saved to disk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import json
from pathlib import Path
import secrets
from typing import Any, Dict, List, Optional

import logging

from core.config import CONFIG, DEFAULT_KEYS_FILE
from core.storage import validate_tenant_id

_LOGGER = logging.getLogger("AuthManager")
DEFAULT_KEYS_CONFIG_PATH = DEFAULT_KEYS_FILE


@dataclass
class APIKeyRecord:
    name: str
    key_hash: str
    tenant_id: str
    scopes: List[str] = field(default_factory=lambda: ["user"])

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "key_hash": self.key_hash,
            "tenant_id": self.tenant_id,
            "scopes": list(self.scopes),
        }

    def has_scope(self, required_scope: str) -> bool:
        """Admins inherit all scopes; other users must match required_scope explicitly."""
        if "admin" in self.scopes:
            return True
        return required_scope in self.scopes


class AuthManager:
    """Manages hashed API keys and constant-time credential verification."""

    def __init__(self, keys_file: Optional[Path] = None) -> None:
        self.keys_file = keys_file or (CONFIG.workspace_dir / "keys.json")
        self._records: List[APIKeyRecord] = []
        self._load()

    @staticmethod
    def hash_key(raw_key: str) -> str:
        """Compute SHA-256 digest of an API key string."""
        return hashlib.sha256((raw_key or "").strip().encode("utf-8")).hexdigest()

    @staticmethod
    def _records_from_raw(raw_list: List[Dict[str, Any]]) -> List[APIKeyRecord]:
        records: List[APIKeyRecord] = []
        for item in raw_list:
            if not isinstance(item, dict) or not item.get("key_hash"):
                continue
            raw_tenant = str(item.get("tenant_id", "admin")).strip()
            try:
                t_id = validate_tenant_id(raw_tenant)
            except ValueError:
                t_id = "admin"
            records.append(
                APIKeyRecord(
                    name=str(item.get("name", "Key")),
                    key_hash=str(item.get("key_hash", "")),
                    tenant_id=t_id,
                    scopes=list(item.get("scopes", ["user"])),
                )
            )
        return records

    def _load_default_seed(self) -> List[Dict[str, Any]]:
        if DEFAULT_KEYS_CONFIG_PATH.exists():
            try:
                data = json.loads(DEFAULT_KEYS_CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(data, list) and data:
                    return data
            except Exception as exc:
                _LOGGER.warning("Failed to parse seed keys from %s: %s", DEFAULT_KEYS_CONFIG_PATH, exc)

        generated_admin_key = f"ns-admin-{secrets.token_urlsafe(24)}"
        _LOGGER.warning(
            "INITIALIZED SECURE ADMIN KEY: '%s'. Save this key; only its SHA-256 hash is saved to disk.",
            generated_admin_key,
        )
        return [
            {
                "name": "Generated Administrator",
                "key_hash": self.hash_key(generated_admin_key),
                "tenant_id": "admin",
                "scopes": ["admin", "user"],
            }
        ]

    def _load(self) -> None:
        if not self.keys_file.exists():
            seeds = self._load_default_seed()
            self._records = self._records_from_raw(seeds)
            self._save()
            return

        try:
            raw = json.loads(self.keys_file.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                self._records = self._records_from_raw(raw)
                return
        except Exception as exc:
            _LOGGER.warning("Corrupted keys file %s: %s. Falling back to defaults.", self.keys_file, exc)

        # Fallback to defaults if corrupt
        self._records = self._records_from_raw(self._load_default_seed())
        self._save()

    def _save(self) -> None:
        self.keys_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [rec.to_dict() for rec in self._records]
        self.keys_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def verify_key(self, raw_key: Optional[str]) -> Optional[APIKeyRecord]:
        """Verify raw_key using constant-time hash comparison against registered credentials.

        Returns:
            The matching APIKeyRecord if verified, or None if invalid or missing.
        """
        if not raw_key or not isinstance(raw_key, str):
            return None

        candidate_hash = self.hash_key(raw_key)
        matched_record: Optional[APIKeyRecord] = None

        # Always iterate all records to maintain uniform execution timing
        for rec in self._records:
            if hmac.compare_digest(rec.key_hash, candidate_hash):
                matched_record = rec

        return matched_record

    def create_key(
        self,
        name: str,
        tenant_id: str,
        scopes: Optional[List[str]] = None,
    ) -> tuple[str, APIKeyRecord]:
        """Generate a cryptographically secure random API key, register its hash, and return it.

        Returns:
            (raw_secret_key, record)
        """
        safe_tenant = validate_tenant_id(tenant_id)
        raw_secret = f"ns_{secrets.token_urlsafe(32)}"
        key_hash = self.hash_key(raw_secret)
        record = APIKeyRecord(
            name=name.strip() or f"Key for {safe_tenant}",
            key_hash=key_hash,
            tenant_id=safe_tenant,
            scopes=list(scopes) if scopes else ["user"],
        )
        self._records.append(record)
        self._save()
        return raw_secret, record

    def list_records(self) -> List[Dict[str, Any]]:
        """List registered key metadata with masked hashes for safe operator inspection."""
        return [
            {
                "name": r.name,
                "tenant_id": r.tenant_id,
                "scopes": r.scopes,
                "key_hash_prefix": f"{r.key_hash[:8]}...{r.key_hash[-4:]}",
            }
            for r in self._records
        ]
