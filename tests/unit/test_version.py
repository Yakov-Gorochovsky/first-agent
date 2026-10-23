"""
tests/unit/test_version.py - Unit tests for Version Management & Deployment Tracking.
"""

from pathlib import Path
from unittest.mock import patch

from core.version import (
    VersionInfo,
    _read_semver,
    bump_version,
    get_version_info,
)


def test_get_version_info_structure():
    ver = get_version_info()
    assert isinstance(ver, VersionInfo)
    assert ver.semver
    assert ver.commit
    assert ver.revision >= 0
    assert ver.server_instance_id.startswith("inst-")
    assert ver.uptime_seconds >= 0.0
    assert "v" in ver.display_version

    v_dict = ver.to_dict()
    for key in (
        "semver",
        "revision",
        "commit",
        "branch",
        "commit_date",
        "commit_message",
        "is_dirty",
        "server_instance_id",
        "server_started_at",
        "uptime_seconds",
        "display_version",
    ):
        assert key in v_dict


def test_bump_version_logic(tmp_path: Path):
    tmp_ver = tmp_path / "VERSION"
    tmp_ver.write_text("1.2.3\n", encoding="utf-8")

    with patch("core.version.VERSION_FILE", tmp_ver):
        assert _read_semver() == "1.2.3"

        # Patch bump
        p = bump_version("patch")
        assert p == "1.2.4"
        assert _read_semver() == "1.2.4"

        # Minor bump
        m = bump_version("minor")
        assert m == "1.3.0"
        assert _read_semver() == "1.3.0"

        # Major bump
        maj = bump_version("major")
        assert maj == "2.0.0"
        assert _read_semver() == "2.0.0"


def test_git_metadata_fallback(tmp_path: Path):
    with patch("shutil.which", return_value=None):
        ver = get_version_info()
        assert ver.commit == "unknown"
        assert ver.revision == 0
        assert ver.is_dirty is False
