"""
tests/unit/test_tools.py - Unit tests for universal tools, AST calculator safety, SSRF, and file isolation.
"""

import os
import shutil

import pytest

from core.config import CONFIG, WORKSPACE_DIR
from tools import (
    calculator,
    fetch_url,
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
    orig = CONFIG.unsafe_allow_local_exec
    CONFIG.unsafe_allow_local_exec = True
    try:
        out = run_python("import os; print(os.environ.get('SECRET_TEST_TOKEN', 'SCRUBBED'))")
        assert "CLASSIFIED_VALUE_9999" not in out
        assert "SCRUBBED" in out
    finally:
        CONFIG.unsafe_allow_local_exec = orig
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
    orig = CONFIG.unsafe_allow_local_exec
    CONFIG.unsafe_allow_local_exec = True
    try:
        code = "import subprocess; subprocess.run(['echo', 'escaped'])"
        res = run_python(code)
        assert "PermissionError" in res or "blocked by sandbox policy" in res

        code_os = "import os; os.system('echo escaped')"
        res_os = run_python(code_os)
        assert "PermissionError" in res_os or "blocked by sandbox policy" in res_os
    finally:
        CONFIG.unsafe_allow_local_exec = orig


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


def test_fetch_url_rate_limiting_retry(monkeypatch):
    """Verify that fetch_url automatically retries when receiving an HTTP 429 rate limit."""
    import requests

    call_count = 0

    class DummyResponse:
        def __init__(self, status_code, text, headers=None):
            self.status_code = status_code
            self.text = text
            self.headers = headers or {}

        def raise_for_status(self):
            if self.status_code == 429:
                err = requests.HTTPError("429 Too Many Requests")
                err.response = self
                raise err

    def fake_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return DummyResponse(429, "Too Many Requests", {"retry-after": "0.01"})
        return DummyResponse(200, "<html><body><h1>Target Verified</h1></body></html>")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr("tools._resolve_and_validate_public_url", lambda url: (url, "example.com", "93.184.216.34", 80))

    result = fetch_url("http://example.com/status")
    assert call_count == 2
    assert "Target Verified" in result


@pytest.mark.unit
def test_fetch_url_structural_dom_and_include_raw(monkeypatch) -> None:
    """Verify fetch_url extracts structural DOM metadata and supports include_raw=True."""
    import requests

    html_sample = (
        '<html lang="he" dir="rtl"><head><title>Portal Login</title>'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<script src="/assets/app.bundle.js"></script></head>'
        '<body><form action="/auth" method="post" id="loginForm">'
        '<input type="password" name="pwd" id="pwdInput" required>'
        '<button type="submit" id="loginBtn">Login</button></form></body></html>'
    )

    class DummyResp:
        status_code = 200
        reason = "OK"
        text = html_sample
        headers = {"Content-Type": "text/html"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **kw: DummyResp())
    monkeypatch.setattr("tools._resolve_and_validate_public_url", lambda url: (url, "example.com", "93.184.216.34", 80))

    res_default = fetch_url("https://example.com/login")
    assert "[STRUCTURAL DOM / HTML METADATA]" in res_default
    assert "viewport" in res_default
    assert "/assets/app.bundle.js" in res_default
    assert "loginForm" in res_default
    assert "[RAW RESPONSE BODY SNIPPET]" not in res_default

    res_raw = fetch_url("https://example.com/login", include_raw=True)
    assert "[RAW RESPONSE BODY SNIPPET]" in res_raw
    assert '<form action="/auth"' in res_raw


