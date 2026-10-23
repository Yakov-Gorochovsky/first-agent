"""
tools.py - Security-Hardened Tools & Primitives for the AI Swarm.

Security Controls Built In:
1. AST-Whitelisted Calculator with Exponent DoS Cap (`_safe_pow`).
2. SSRF-Protected `fetch_url` (`_validate_public_url` blocks localhost, RFC-1918 private IPs,
   and 169.254.169.254 cloud metadata endpoints).
3. Secret-Scrubbed Python Sandbox (`_get_sandbox_env` strips API_KEY/TOKEN/SECRET env vars
   before launching the child Python process).
4. Strict Path Traversal Prevention (`_safe_workspace_path` confines all file I/O to `workspace/`).
5. Subprocess Network Confinement & SSRF Firewall in `run_python` (`_SUBPROCESS_SANDBOX_BOOTSTRAP`
   blocks outbound loopback, RFC-1918 private IPs, and link-local metadata connections inside child processes).
"""

import ast
import datetime
import ipaddress
import math
import operator
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests

from core.config import CONFIG, WORKSPACE_DIR
from core.resilience import RetryConfig, retry_with_backoff
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

_DDG_LINK_RE = re.compile(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_DDG_SNIPPET_RE = re.compile(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


# ---------------------------------------------------------------------------
# Security & HTML Helpers
# ---------------------------------------------------------------------------

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


class _TextExtractor(HTMLParser):
    """Strip HTML tags, scripts, and styles to extract readable text."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript", "svg", "head"}:
            self._skip_depth += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "head"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self._chunks.append(cleaned + " ")

    def get_text(self) -> str:
        raw = "".join(self._chunks)
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _html_to_text(html_content: str) -> str:
    parser = _TextExtractor()
    parser.feed(html_content)
    return parser.get_text()


# ---------------------------------------------------------------------------
# 1. Tool Implementations
# ---------------------------------------------------------------------------

def _safe_pow(base: float, exp: float) -> float:
    if abs(exp) > CONFIG.calc_max_exponent:
        raise ValueError(f"Exponent {exp} exceeds safe limit ({CONFIG.calc_max_exponent}).")
    if abs(base) > 1_000_000 and abs(exp) > 20:
        raise ValueError("Base and exponent combination too large.")
    return float(operator.pow(base, exp))


def _safe_factorial(n: Any) -> int:
    try:
        val = int(n)
    except Exception:
        raise ValueError("factorial(n) argument must be an integer.")
    if val < 0 or val > 1000:
        raise ValueError("factorial(n) input must be between 0 and 1000.")
    return math.factorial(val)


def _safe_comb(n: Any, k: Any) -> int:
    try:
        n_val, k_val = int(n), int(k)
    except Exception:
        raise ValueError("comb(n, k) arguments must be integers.")
    if n_val < 0 or n_val > 1000 or k_val < 0:
        raise ValueError("comb(n, k) input must be non-negative with n <= 1000.")
    return math.comb(n_val, k_val)


def _safe_perm(n: Any, k: Any = None) -> int:
    try:
        n_val = int(n)
        k_val = int(k) if k is not None else None
    except Exception:
        raise ValueError("perm(n, k) arguments must be integers.")
    if n_val < 0 or n_val > 1000 or (k_val is not None and k_val < 0):
        raise ValueError("perm(n, k) input must be non-negative with n <= 1000.")
    return math.perm(n_val, k_val)


_ALLOWED_MATH_NAMES: Dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
    "nan": math.nan,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "exp": math.exp,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "ceil": math.ceil,
    "floor": math.floor,
    "trunc": math.trunc,
    "abs": abs,
    "round": round,
    "factorial": _safe_factorial,
    "comb": _safe_comb,
    "perm": _safe_perm,
    "degrees": math.degrees,
    "radians": math.radians,
    "hypot": math.hypot,
    "gcd": math.gcd,
    "lcm": math.lcm,
}

_ALLOWED_CALC_OPERATORS: Dict[type, Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: _safe_pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def calculator(expression: str) -> str:
    """Safely evaluate a mathematical expression using a strict AST whitelist."""
    expr_clean = expression.strip()
    if len(expr_clean) > 300:
        return f"Error evaluating '{expression}': expression length exceeds safe limit (300 chars)."

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_CALC_OPERATORS:
            return _ALLOWED_CALC_OPERATORS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_CALC_OPERATORS:
            return _ALLOWED_CALC_OPERATORS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name) and node.id in _ALLOWED_MATH_NAMES:
            return _ALLOWED_MATH_NAMES[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in _ALLOWED_MATH_NAMES and callable(_ALLOWED_MATH_NAMES[func_name]):
                args = [_eval(arg) for arg in node.args]
                return _ALLOWED_MATH_NAMES[func_name](*args)
        raise ValueError(f"Unsupported math expression: {expression}")

    try:
        parsed = ast.parse(expr_clean, mode="eval")
        result = _eval(parsed)
        return f"{expression} = {result}"
    except Exception as exc:
        return f"Error evaluating '{expression}': {exc}"


def get_current_time() -> str:
    """Return the current local date, time, and day of the week."""
    now = datetime.datetime.now().astimezone()
    return now.strftime("%A, %Y-%m-%d %H:%M:%S (%Z)")


def get_weather(city: str) -> str:
    """Fetch current weather for a city using wttr.in (no API key needed)."""
    try:
        url = f"https://wttr.in/{requests.utils.quote(city)}?format=j1"
        resp = requests.get(
            url,
            timeout=CONFIG.weather_timeout_sec,
            headers={"User-Agent": "first-agent/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
        current = data["current_condition"][0]
        desc = current["weatherDesc"][0]["value"]
        temp_c = current["temp_C"]
        feels_c = current["FeelsLikeC"]
        humidity = current["humidity"]
        return (
            f"Weather in {city}: {desc}, {temp_c}°C "
            f"(feels like {feels_c}°C), Humidity: {humidity}%"
        )
    except Exception as exc:
        return f"Could not fetch live weather for '{city}': {exc}"


def web_search(query: str) -> str:
    """Search the web using DuckDuckGo HTML and return top results."""
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }

        def _do_search() -> requests.Response:
            r = requests.get(url, headers=headers, timeout=CONFIG.search_timeout_sec)
            r.raise_for_status()
            return r

        resp = retry_with_backoff(
            _do_search,
            retry_config=RetryConfig(max_retries=3, initial_delay=1.0, max_delay=15.0),
            retryable_exceptions=(requests.RequestException,),
        )
        html = resp.text

        links = _DDG_LINK_RE.findall(html)
        snippets = _DDG_SNIPPET_RE.findall(html)

        results = []
        for idx, (raw_href, raw_title) in enumerate(links[: CONFIG.search_max_results], start=1):
            title = _html_to_text(raw_title).strip()
            snippet = _html_to_text(snippets[idx - 1]).strip() if idx - 1 < len(snippets) else ""
            parsed_href = urlparse(raw_href)
            qs = parse_qs(parsed_href.query)
            clean_url = unquote(qs["uddg"][0]) if "uddg" in qs else raw_href
            results.append(f"{idx}. {title}\n   URL: {clean_url}\n   Snippet: {snippet}")

        if not results:
            return f"No web results found for '{query}'."
        return "\n\n".join(results)
    except Exception as exc:
        return f"Error searching the web for '{query}': {exc}"


def fetch_url(url: str) -> str:
    """Fetch a public webpage URL (SSRF & DNS-rebinding protected) and return readable text content."""
    try:
        safe_url, hostname, pinned_ip, port = _resolve_and_validate_public_url(url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        with _pinned_getaddrinfo(hostname, pinned_ip):
            def _do_fetch() -> requests.Response:
                r = requests.get(
                    safe_url,
                    headers=headers,
                    timeout=CONFIG.fetch_timeout_sec,
                    allow_redirects=False,
                )
                r.raise_for_status()
                return r

            resp = retry_with_backoff(
                _do_fetch,
                retry_config=RetryConfig(max_retries=3, initial_delay=1.0, max_delay=15.0),
                retryable_exceptions=(requests.RequestException,),
            )
        from core.cleaning import clean_html_to_markdown
        text = clean_html_to_markdown(resp.text, max_chars=CONFIG.fetch_max_chars)
        status_line = f"HTTP {getattr(resp, 'status_code', 200)} {getattr(resp, 'reason', '')}".strip()
        headers_lines = [f"{k}: {v}" for k, v in getattr(resp, "headers", {}).items()]
        headers_block = "\n".join(headers_lines[:30])
        content_preview = text if text else "No readable text content found."
        return (
            f"[HTTP RESPONSE: {status_line}]\n"
            f"[RESPONSE HEADERS]\n{headers_block}\n\n"
            f"[PAGE CONTENT]\n{content_preview}"
        )
    except Exception as exc:
        return f"Error fetching URL '{url}': {exc}"


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


def run_python(code: str) -> str:
    """Execute Python code in an isolated container sandbox or fail closed unless local execution is permitted."""
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
            sandboxed_script = (
                f"{_SUBPROCESS_SANDBOX_BOOTSTRAP}\n"
                f"exec(compile({repr(code)}, '<sandbox>', 'exec'), globals())\n"
            )
            completed = subprocess.run(
                [sys.executable, "-c", sandboxed_script],
                cwd=str(WORKSPACE_DIR),
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


# ---------------------------------------------------------------------------
# 2. Tool Registry & Schemas
# ---------------------------------------------------------------------------

AVAILABLE_TOOLS: Dict[str, Callable[..., str]] = {
    "calculator": calculator,
    "get_current_time": get_current_time,
    "get_weather": get_weather,
    "web_search": web_search,
    "fetch_url": fetch_url,
    "run_python": run_python,
    "remember_fact": remember_fact,
    "recall_memories": recall_memories,
    "save_note": save_note,
    "read_note": read_note,
    "list_notes": list_notes,
}

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a math expression (e.g., '24 * 17 + sqrt(144)').",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The mathematical expression to evaluate.",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current local date, time, and day of the week.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current live weather for a given city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "City name, e.g., 'Tel Aviv', 'London', 'New York'.",
                    }
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the internet via DuckDuckGo for up-to-date facts, news, or documentation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query string.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "Fetch and extract the readable text content of any public HTTP/HTTPS web page URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full public HTTP/HTTPS URL to read.",
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Execute Python 3 code in a sandboxed subprocess (working directory: workspace/) "
                "and return stdout/stderr."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Valid Python 3 code to run. Use print() to output results.",
                    }
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Store an important fact or user preference in persistent long-term memory across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Short label/topic for the memory (e.g., 'user_name').",
                    },
                    "fact": {
                        "type": "string",
                        "description": "The details to remember.",
                    },
                },
                "required": ["key", "fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall_memories",
            "description": "List all facts stored in persistent long-term memory.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Save a text note or file into the agent's workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the file to save (e.g., 'todo.txt').",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write into the file.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_note",
            "description": "Read the contents of a saved note from the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the file to read (e.g., 'todo.txt').",
                    }
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_notes",
            "description": "List all saved files in the workspace directory.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


def execute_tool(name: str, arguments: Dict[str, Any]) -> str:
    """Run a registered tool by name and return its string output."""
    func = AVAILABLE_TOOLS.get(name)
    if not func:
        return f"Error: Unknown tool '{name}'. Available tools: {list(AVAILABLE_TOOLS)}"
    try:
        return str(func(**(arguments or {})))
    except Exception as exc:
        return f"Error running tool '{name}': {exc}"
