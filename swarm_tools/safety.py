"""swarm_tools/safety.py - Path, URL, and environment checks shared by the tools."""

import ipaddress
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

from core.config import WORKSPACE_DIR
from core.storage import sanitize_tenant_id

_SENSITIVE_ENV_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")

_RESTRICTED_FILENAMES = {
    ".env",
    "keys.json",
    "swarm.db",
    "swarms.json",
    "roles.json",
    "memory.json",
}


def _safe_workspace_path(filename: str, tenant_id: Optional[str] = None) -> Path:
    """Resolve filename strictly inside WORKSPACE_DIR (or tenant folder), blocking traversal and system files."""
    raw_path = Path(filename.strip().replace("\\", "/"))
    clean_name = raw_path.name
    lower_name = clean_name.lower()

    if not clean_name or clean_name in {".", ".."} or clean_name.startswith("."):
        raise ValueError(f"Invalid or restricted filename: '{filename}'")

    if (
        lower_name in _RESTRICTED_FILENAMES
        or lower_name.startswith(".env")
        or lower_name.endswith((".db", ".db-wal", ".db-shm"))
    ):
        raise ValueError(f"Restricted system file cannot be accessed: '{filename}'")

    ws_resolved = WORKSPACE_DIR.resolve()
    if tenant_id:
        safe_tenant = sanitize_tenant_id(tenant_id)
        base_dir = (ws_resolved / "tenants" / safe_tenant).resolve()
        base_dir.mkdir(parents=True, exist_ok=True)
    else:
        base_dir = ws_resolved

    target = (base_dir / clean_name).resolve()
    if not target.is_relative_to(ws_resolved) or target.parent != base_dir:
        raise ValueError(f"Path traversal blocked for '{filename}'")
    return target


@contextmanager
def _pinned_getaddrinfo(hostname: str, pinned_ip: str):
    """Context manager that forces socket.getaddrinfo to resolve hostname strictly to pinned_ip, preventing DNS rebinding."""
    orig_getaddrinfo = socket.getaddrinfo

    def _custom_getaddrinfo(host, port, *args, **kwargs):
        if str(host).lower() == hostname.lower():
            return orig_getaddrinfo(pinned_ip, port, *args, **kwargs)
        return orig_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = _custom_getaddrinfo
    try:
        yield
    finally:
        socket.getaddrinfo = orig_getaddrinfo


def _resolve_and_validate_public_url(url: str) -> tuple[str, str, str, int]:
    """Validate URL scheme and resolve IP, verifying non-loopback, non-private, and non-metadata address."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Blocked URL scheme '{parsed.scheme}'. Only http/https are allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: missing hostname.")
    if hostname.lower() in {"localhost", "0.0.0.0"}:
        raise ValueError("SSRF blocked: localhost access is not permitted.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addr_info = socket.getaddrinfo(hostname, port)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname '{hostname}': {exc}") from exc

    valid_ip: Optional[str] = None
    for entry in addr_info:
        ip_str = str(entry[4][0])
        ip_obj = ipaddress.ip_address(ip_str)
        if (
            ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_reserved
            or ip_obj.is_multicast
            or ip_obj.is_unspecified
        ):
            raise ValueError(
                f"SSRF protection blocked access to non-public IP address ({ip_str}) for '{hostname}'."
            )
        if valid_ip is None:
            valid_ip = ip_str

    if valid_ip is None:
        raise ValueError(f"No valid public IP found for '{hostname}'.")
    return url.strip(), hostname, valid_ip, port


def _validate_public_url(url: str) -> str:
    """Validate URL scheme and block SSRF against localhost, private networks, and metadata IPs."""
    safe_url, _, _, _ = _resolve_and_validate_public_url(url)
    return safe_url


def _get_sandbox_env() -> Dict[str, str]:
    """Return a copy of os.environ with all API keys, tokens, and secrets stripped."""
    safe_env: Dict[str, str] = {}
    for k, v in os.environ.items():
        upper_k = k.upper()
        if any(sub in upper_k for sub in _SENSITIVE_ENV_SUBSTRINGS):
            continue
        safe_env[k] = v
    safe_env["PYTHONIOENCODING"] = "utf-8"
    return safe_env
