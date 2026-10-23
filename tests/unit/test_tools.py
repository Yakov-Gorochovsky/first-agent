"""
tests/unit/test_tools.py - Unit tests for universal tools, AST calculator safety, SSRF, and file isolation.
"""

from pathlib import Path
import os
import shutil
import pytest

from core.config import WORKSPACE_DIR
from tools import (
    calculator,
    execute_tool,
    fetch_url,
    list_notes,
    read_note,
    run_python,
    save_note,
)


@pytest.mark.unit
@pytest.mark.security
def test_calculator_ast_evaluation_and_safety() -> None:
    """Calculator computes arithmetic and mathematical functions while rejecting malicious AST nodes."""
    # Benign expressions
    assert "153" in calculator("12 * 12 + sqrt(81)")
    assert "120" in calculator("factorial(5)")
    assert "10" in calculator("comb(5, 2)")

    # Malicious injection attempts
    assert "Error" in calculator("__import__('os').system('dir')")
    assert "Error" in calculator("open('/etc/passwd')")

    # Exponent DoS
    assert "exceeds safe limit" in calculator("999999 ** 999999")

    # Factorial algorithmic DoS
    assert "between 0 and 1000" in calculator("factorial(10000000)")

    # Expression length limit (> 300 characters)
    long_expr = "1 + " * 200 + "1"
    assert "expression length exceeds safe limit" in calculator(long_expr)


@pytest.mark.unit
@pytest.mark.security
def test_fetch_url_ssrf_protection() -> None:
    """fetch_url blocks loopback, cloud metadata, and RFC1918 addresses."""
    out_state = fetch_url("http://127.0.0.1:8000/api/state")
    assert "SSRF" in out_state and "blocked" in out_state.lower()

    out_meta = fetch_url("http://169.254.169.254/latest/meta-data/")
    assert "SSRF" in out_meta and "blocked" in out_meta.lower()

    out_local = fetch_url("http://localhost:8080")
    assert "SSRF" in out_local and "blocked" in out_local.lower()


@pytest.mark.unit
@pytest.mark.security
def test_run_python_environment_secret_scrubbing() -> None:
    """Child Python processes must not inherit host secrets or API keys."""
    os.environ["SECRET_TEST_TOKEN"] = "CLASSIFIED_VALUE_9999"
    try:
        out = run_python("import os; print(os.environ.get('SECRET_TEST_TOKEN', 'SCRUBBED'))")
        assert "CLASSIFIED_VALUE_9999" not in out
        assert "SCRUBBED" in out
    finally:
        os.environ.pop("SECRET_TEST_TOKEN", None)


@pytest.mark.unit
@pytest.mark.security
def test_notes_multi_tenant_isolation_and_restricted_files() -> None:
    """Workspace notes must scope to tenant and reject path traversal and system files."""
    tenant_a = "test_tenant_a"
    tenant_b = "test_tenant_b"

    try:
        # Tenant isolation
        save_note("memo.txt", "Tenant A Memo", tenant_id=tenant_a)
        save_note("memo.txt", "Tenant B Memo", tenant_id=tenant_b)

        assert read_note("memo.txt", tenant_id=tenant_a) == "Tenant A Memo"
        assert read_note("memo.txt", tenant_id=tenant_b) == "Tenant B Memo"

        # Restricted system file protection
        assert "Restricted system file" in save_note("keys.json", "bad", tenant_id=tenant_a)
        assert "Restricted system file" in save_note("swarm.db", "bad", tenant_id=tenant_a)
        assert "Invalid or restricted filename" in save_note(".env", "bad", tenant_id=tenant_a)

        # Directory traversal blocked
        traversal = save_note("../../outside.txt", "bad", tenant_id=tenant_a)
        assert "outside.txt" in traversal
    finally:
        shutil.rmtree(WORKSPACE_DIR / "tenants" / tenant_a, ignore_errors=True)
        shutil.rmtree(WORKSPACE_DIR / "tenants" / tenant_b, ignore_errors=True)
        (WORKSPACE_DIR / "outside.txt").unlink(missing_ok=True)


def test_run_python_process_breakout_blocked():
    """Verify FP-01: child interpreter / process spawning from within sandbox is blocked."""
    code = "import subprocess; subprocess.run(['echo', 'escaped'])"
    res = run_python(code)
    assert "PermissionError" in res or "blocked by sandbox policy" in res

    code_os = "import os; os.system('echo escaped')"
    res_os = run_python(code_os)
    assert "PermissionError" in res_os or "blocked by sandbox policy" in res_os


def test_run_python_fail_closed_mode(monkeypatch):
    """Verify FP-01: fails closed if container is absent and unsafe local execution is disabled."""
    import shutil
    import core.config

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    monkeypatch.setattr(core.config.CONFIG, "unsafe_allow_local_exec", False)
    monkeypatch.setattr(core.config.CONFIG, "python_sandbox_mode", "auto")

    res = run_python("print('should fail closed')")
    assert "SecurityError" in res
    assert "Subprocess execution failed closed" in res
