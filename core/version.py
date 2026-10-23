"""
core/version.py - Version Management & Deployment Tracking.

Provides:
- Semantic version tracking with automatic Git commit hash & revision detection.
- Dirty working tree detection (* flag if code has uncommitted modifications).
- Server instance lifetime and uptime tracking.
- Version bump utility (major, minor, patch).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

logger = logging.getLogger("neural_swarm.version")

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "VERSION"
DEFAULT_SEMVER = "0.5.0"
SERVER_START_TIME = time.time()
_SERVER_INSTANCE_ID = f"inst-{int(SERVER_START_TIME)}-{os.getpid()}"


def _read_semver() -> str:
    """Read semver from VERSION file or return default."""
    if VERSION_FILE.exists():
        try:
            content = VERSION_FILE.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception:
            pass
    return DEFAULT_SEMVER


def _write_semver(new_version: str) -> None:
    """Persist updated semver to VERSION file."""
    VERSION_FILE.write_text(new_version.strip() + "\n", encoding="utf-8")


def _get_git_metadata() -> Tuple[int, str, str, str, str, bool]:
    """
    Query current git state:
    returns (revision_count, commit_hash, branch, commit_date, commit_message, is_dirty)
    """
    git_bin = shutil.which("git")
    if not git_bin:
        return 0, "unknown", "unknown", "", "", False

    try:
        # Revision count
        rev_res = subprocess.run(
            [git_bin, "rev-list", "--count", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        rev_count = (
            int(rev_res.stdout.strip())
            if rev_res.returncode == 0 and rev_res.stdout.strip().isdigit()
            else 0
        )

        # Short commit hash
        hash_res = subprocess.run(
            [git_bin, "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        commit_hash = hash_res.stdout.strip() if hash_res.returncode == 0 else "unknown"

        # Branch
        branch_res = subprocess.run(
            [git_bin, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        branch = branch_res.stdout.strip() if branch_res.returncode == 0 else "main"

        # Commit date & subject
        log_res = subprocess.run(
            [git_bin, "log", "-1", "--pretty=format:%cI%x09%s"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        commit_date, commit_msg = "", ""
        if log_res.returncode == 0 and "\t" in log_res.stdout:
            parts = log_res.stdout.split("\t", 1)
            commit_date = parts[0].strip()
            commit_msg = parts[1].strip()

        # Dirty status
        status_res = subprocess.run(
            [git_bin, "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        is_dirty = bool(status_res.stdout.strip()) if status_res.returncode == 0 else False

        return rev_count, commit_hash, branch, commit_date, commit_msg, is_dirty
    except Exception as exc:
        logger.debug("Failed querying git metadata: %s", exc)
        return 0, "unknown", "unknown", "", "", False


@dataclass
class VersionInfo:
    semver: str
    revision: int
    commit: str
    branch: str
    commit_date: str
    commit_message: str
    is_dirty: bool
    server_instance_id: str
    server_started_at: float
    uptime_seconds: float
    display_version: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_version_info() -> VersionInfo:
    """Compute current dynamic version snapshot."""
    semver = _read_semver()
    rev, commit, branch, c_date, c_msg, is_dirty = _get_git_metadata()

    dirty_suffix = "*" if is_dirty else ""
    rev_str = f"rev{rev}" if rev > 0 else "dev"
    hash_str = f"{commit}{dirty_suffix}"
    display = f"v{semver}-{rev_str} ({hash_str})"

    uptime = round(time.time() - SERVER_START_TIME, 1)

    return VersionInfo(
        semver=semver,
        revision=rev,
        commit=commit,
        branch=branch,
        commit_date=c_date,
        commit_message=c_msg,
        is_dirty=is_dirty,
        server_instance_id=_SERVER_INSTANCE_ID,
        server_started_at=SERVER_START_TIME,
        uptime_seconds=uptime,
        display_version=display,
    )


def bump_version(part: str = "patch") -> str:
    """
    Increment semantic version in VERSION file.
    part: 'major' | 'minor' | 'patch'
    """
    current = _read_semver()
    parts = current.split(".")
    while len(parts) < 3:
        parts.append("0")

    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        major, minor, patch = 0, 5, 0

    if part == "major":
        major += 1
        minor = 0
        patch = 0
    elif part == "minor":
        minor += 1
        patch = 0
    else:  # patch
        patch += 1

    new_version = f"{major}.{minor}.{patch}"
    _write_semver(new_version)
    return new_version
