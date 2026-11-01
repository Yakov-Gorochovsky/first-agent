"""swarm_tools/python_exec.py - run_python: container isolation, failing closed without one."""

import ast
import logging
import secrets
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional

from core.config import CONFIG
from swarm_tools.safety import _get_sandbox_env

logger = logging.getLogger("neural_swarm.tools")

_SUBPROCESS_SANDBOX_BOOTSTRAP = '''
import socket
import ipaddress
import sys
import os
import subprocess

# Defense 1: Block child process spawning / interpreter breakouts
def _blocked_exec(*args, **kwargs):
    raise PermissionError("Process spawning (subprocess/exec/system) is blocked by sandbox policy.")

subprocess.Popen = _blocked_exec
subprocess.run = _blocked_exec
subprocess.call = _blocked_exec
subprocess.check_call = _blocked_exec
subprocess.check_output = _blocked_exec
os.system = _blocked_exec
os.popen = _blocked_exec
for _attr in ["execv", "execve", "execl", "execle", "execlp", "execlpe", "execvp", "execvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe"]:
    if hasattr(os, _attr):
        setattr(os, _attr, _blocked_exec)

_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex

def _check_sandbox_addr(address):
    try:
        if isinstance(address, tuple) and len(address) >= 2:
            host, port = address[0], address[1]
        else:
            return
        if not host:
            return
        host_str = str(host).strip().lower()
        if host_str in {"localhost", "0.0.0.0", "::1", "127.0.0.1", "169.254.169.254"}:
            raise PermissionError(f"SSRF Protection: Outbound connection to '{host}:{port}' is blocked by sandbox policy.")
        try:
            ip = ipaddress.ip_address(host_str)
            if (
                ip.is_loopback
                or ip.is_private
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise PermissionError(f"SSRF Protection: Outbound connection to non-public IP ({ip}:{port}) is blocked.")
        except ValueError:
            try:
                for entry in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP):
                    target_ip = ipaddress.ip_address(entry[4][0])
                    if (
                        target_ip.is_loopback
                        or target_ip.is_private
                        or target_ip.is_link_local
                        or target_ip.is_reserved
                        or target_ip.is_multicast
                        or target_ip.is_unspecified
                    ):
                        raise PermissionError(
                            f"SSRF Protection: Host '{host}' resolves to non-public IP ({target_ip}:{port}) and is blocked."
                        )
            except socket.gaierror:
                pass
    except PermissionError:
        raise
    except Exception:
        pass

def _guarded_connect(self, address):
    _check_sandbox_addr(address)
    return _orig_connect(self, address)

def _guarded_connect_ex(self, address):
    _check_sandbox_addr(address)
    return _orig_connect_ex(self, address)

socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex
'''


def _is_misused_file_read(code_str: str) -> Optional[str]:
    """Deterministic AST Guard: Block run_python from being misused purely for reading or listing workspace files.
    Agents must use 'read_note' and 'list_notes' instead, which are fast, tenant-scoped, and require no human approval."""
    try:
        tree = ast.parse(code_str)
    except Exception:
        return None

    # Legitimate test frameworks, heavy analytical libraries, or class definitions are exempted
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])] + [getattr(node, "module", "") or ""]
            if any(n in ("unittest", "pytest", "numpy", "pandas", "scipy", "requests", "bs4", "selenium", "sqlite3") for n in names):
                return None
        elif isinstance(node, ast.ClassDef):
            return None

    reads_files = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "open":
                reads_files = True
            elif isinstance(func, ast.Attribute) and func.attr in ("listdir", "walk", "glob", "read_text", "read_bytes"):
                reads_files = True

    if reads_files:
        has_functions = any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree))
        if not has_functions:
            return (
                "PolicyViolation: 'run_python' cannot be used purely to read or list workspace files. "
                "Use the dedicated 'read_note(filename)' and 'list_notes()' tools instead. "
                "They are instantaneous, tenant-scoped, and require no human approval."
            )
    return None


def run_python(code: str) -> str:
    """Execute Python code in an isolated container sandbox or fail closed unless local execution is permitted."""
    guard_err = _is_misused_file_read(code)
    if guard_err:
        return guard_err

    container_bin = shutil.which("docker") or shutil.which("podman")
    use_container = container_bin is not None and CONFIG.python_sandbox_mode in {"auto", "container"}

    if not use_container:
        if not CONFIG.unsafe_allow_local_exec and CONFIG.python_sandbox_mode != "local":
            return (
                "SecurityError: True container isolation (Docker/Podman) is unavailable on this host. "
                "Subprocess execution failed closed to prevent host compromise. "
                "To permit local execution for development, enable UNSAFE_ALLOW_LOCAL_EXEC=true in your .env."
            )

    container_name = f"swarm-exec-{secrets.token_hex(8)}"
    try:
        if use_container:
            assert container_bin is not None
            cmd: List[str] = [
                container_bin,
                "run",
                "--name", container_name,
                "--rm",
                "--user", "1000:1000",
                "--network", "none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--tmpfs", "/workspace:rw,size=64m",
                "--tmpfs", "/tmp:rw,size=32m",
                "--memory", "256m",
                "--cpus", "1.0",
                "--pids-limit", "64",
                "-w", "/workspace",
                "python:3.11-slim",
                "python", "-c", code,
            ]
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CONFIG.python_timeout_sec,
            )
        else:
            logger.warning("run_python is using UNSAFE local execution: this is a guardrail, not a sandbox.")
            sandboxed_script = (
                f"{_SUBPROCESS_SANDBOX_BOOTSTRAP}\n"
                f"exec(compile({repr(code)}, '<sandbox>', 'exec'), {{'__name__': '__main__'}})\n"
            )
            with tempfile.TemporaryDirectory(prefix="swarm-exec-") as scratch_dir:
                completed = subprocess.run(
                    [sys.executable, "-c", sandboxed_script],
                    cwd=scratch_dir,
                    env=_get_sandbox_env(),
                    capture_output=True,
                    text=True,
                    timeout=CONFIG.python_timeout_sec,
                )

        stdout = completed.stdout.strip()
        stderr = completed.stderr.strip()
        from core.cleaning import summarize_code_output
        return summarize_code_output(
            stdout, stderr, max_chars=CONFIG.python_max_output_chars
        )
    except subprocess.TimeoutExpired:
        if use_container and container_bin:
            try:
                subprocess.run([container_bin, "rm", "-f", container_name], capture_output=True, timeout=5)
            except Exception:
                pass
        return f"Error: Python execution timed out after {CONFIG.python_timeout_sec} seconds."
    except Exception as exc:
        if use_container and container_bin:
            try:
                subprocess.run([container_bin, "rm", "-f", container_name], capture_output=True, timeout=5)
            except Exception:
                pass
        return f"Error running Python code: {exc}"
