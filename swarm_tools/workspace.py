"""swarm_tools/workspace.py - Tenant-scoped notes and the fallback memory tools."""

from typing import Optional

from core.config import WORKSPACE_DIR
from core.storage import sanitize_tenant_id
from swarm_tools.safety import _RESTRICTED_FILENAMES, _safe_workspace_path


def remember_fact(key: str, fact: str, tenant_id: str = "default") -> str:
    """Save or update a persistent fact in multi-tenant SQLite storage."""
    from core.storage import SQLiteStorage
    storage = SQLiteStorage()
    storage.save_tenant_memory(tenant_id, key.strip(), fact.strip())
    return f"Remembered '{key}': {fact}"


def recall_memories(tenant_id: str = "default") -> str:
    """Retrieve all facts stored in persistent multi-tenant SQLite storage."""
    from core.storage import SQLiteStorage
    storage = SQLiteStorage()
    mems = storage.get_tenant_memories(tenant_id)
    if not mems:
        return "Long-term memory is currently empty."
    lines = [f"- {k}: {v}" for k, v in mems.items()]
    return "Stored memories:\n" + "\n".join(lines)


def save_note(filename: str, content: str, tenant_id: Optional[str] = None) -> str:
    """Save text content to a file strictly inside the agent's workspace folder (or tenant workspace)."""
    try:
        target = _safe_workspace_path(filename, tenant_id=tenant_id)
        target.write_text(content, encoding="utf-8")
        rel_path = target.relative_to(WORKSPACE_DIR)
        return f"Saved {len(content)} characters to workspace/{rel_path}"
    except Exception as exc:
        return f"Error saving note '{filename}': {exc}"


def read_note(filename: str, tenant_id: Optional[str] = None) -> str:
    """Read text content from a file strictly inside the agent's workspace folder (or tenant workspace)."""
    try:
        target = _safe_workspace_path(filename, tenant_id=tenant_id)
        if not target.exists():
            rel_path = target.relative_to(WORKSPACE_DIR)
            return f"File 'workspace/{rel_path}' does not exist."
        return target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading note '{filename}': {exc}"


def list_notes(tenant_id: Optional[str] = None) -> str:
    """List all files saved inside the agent's workspace folder (or tenant workspace)."""
    ws_resolved = WORKSPACE_DIR.resolve()
    if tenant_id:
        safe_tenant = sanitize_tenant_id(tenant_id)
        base_dir = ws_resolved / "tenants" / safe_tenant
        if not base_dir.exists():
            return f"The workspace folder for '{safe_tenant}' is currently empty."
    else:
        base_dir = ws_resolved

    files = sorted(
        p.name
        for p in base_dir.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.name.lower() not in _RESTRICTED_FILENAMES
        and not p.name.lower().endswith((".db", ".db-wal", ".db-shm"))
    )
    if not files:
        label = f" for '{tenant_id}'" if tenant_id else ""
        return f"The workspace folder{label} is currently empty."
    header = f"Files in workspace [{tenant_id}]: " if tenant_id else "Files in workspace: "
    return header + ", ".join(files)
