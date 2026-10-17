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
from html.parser import HTMLParser
import ipaddress
import json
import math
import operator
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
from typing import Any, Callable, Dict, List
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
import requests

from core.config import CONFIG, WORKSPACE_DIR

MEMORY_FILE = WORKSPACE_DIR / "memory.json"

_SENSITIVE_ENV_SUBSTRINGS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "AUTH", "CREDENTIAL")


# ---------------------------------------------------------------------------
# Security & HTML Helpers
# ---------------------------------------------------------------------------

def _safe_workspace_path(filename: str) -> Path:
    """Resolve filename strictly inside WORKSPACE_DIR, blocking path traversal."""
    clean_name = Path(filename).name
    if not clean_name or clean_name in {".", ".."} or clean_name.startswith(".env"):
        raise ValueError(f"Invalid or restricted filename: '{filename}'")
    ws_resolved = WORKSPACE_DIR.resolve()
    target = (ws_resolved / clean_name).resolve()
    if target.parent != ws_resolved:
        raise ValueError(f"Path traversal blocked for '{filename}'")
    return target


def _validate_public_url(url: str) -> str:
    """Validate URL scheme and block SSRF against localhost, private networks, and metadata IPs."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Blocked URL scheme '{parsed.scheme}'. Only http/https are allowed.")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL: missing hostname.")
    if hostname.lower() in {"localhost", "0.0.0.0"}:
        raise ValueError("SSRF blocked: localhost access is not permitted.")

    try:
        addr_info = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve hostname '{hostname}': {exc}") from exc

    for entry in addr_info:
        ip_str = entry[4][0]
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
    return url.strip()


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


def calculator(expression: str) -> str:
    """Safely evaluate a mathematical expression using a strict AST whitelist."""
    allowed_names = {
        k: v for k, v in math.__dict__.items() if not k.startswith("__")
    }
    allowed_operators = {
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

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in allowed_operators:
            return allowed_operators[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in allowed_operators:
            return allowed_operators[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name) and node.id in allowed_names:
            return allowed_names[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name in allowed_names and callable(allowed_names[func_name]):
                args = [_eval(arg) for arg in node.args]
                return allowed_names[func_name](*args)
        raise ValueError(f"Unsupported math expression: {expression}")

    try:
        parsed = ast.parse(expression.strip(), mode="eval")
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
        resp = requests.get(url, headers=headers, timeout=CONFIG.search_timeout_sec)
        resp.raise_for_status()
        html = resp.text

        link_pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        links = link_pattern.findall(html)
        snippets = snippet_pattern.findall(html)

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
    """Fetch a public webpage URL (SSRF-protected) and return readable text content."""
    try:
        safe_url = _validate_public_url(url)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(
            safe_url,
            headers=headers,
            timeout=CONFIG.fetch_timeout_sec,
            allow_redirects=False,
        )
        resp.raise_for_status()
        text = _html_to_text(resp.text)
        if not text:
            return f"Fetched {safe_url}, but no readable text was found."
        max_chars = CONFIG.fetch_max_chars
        if len(text) > max_chars:
            return text[:max_chars] + f"\n... [Truncated {len(text) - max_chars} chars]"
        return text
    except Exception as exc:
        return f"Error fetching URL '{url}': {exc}"


_SUBPROCESS_SANDBOX_BOOTSTRAP = '''
import socket
import ipaddress

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
    """Execute Python code in a secret-scrubbed, network-confined subprocess inside workspace/."""
    try:
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
        parts = []
        if stdout:
            parts.append(f"STDOUT:\n{stdout}")
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        if not parts:
            parts.append(f"Code executed successfully with exit code {completed.returncode} (no output).")
        elif completed.returncode != 0:
            parts.append(f"(Exit code: {completed.returncode})")
        output = "\n".join(parts)
        return output[: CONFIG.python_max_output_chars]
    except subprocess.TimeoutExpired:
        return f"Error: Python execution timed out after {CONFIG.python_timeout_sec} seconds."
    except Exception as exc:
        return f"Error running Python code: {exc}"


def load_memories() -> Dict[str, str]:
    """Load persistent key-value memories from workspace/memory.json."""
    if not MEMORY_FILE.exists():
        return {}
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def remember_fact(key: str, fact: str) -> str:
    """Save or update a persistent fact in long-term memory across sessions."""
    memories = load_memories()
    memories[key.strip()] = fact.strip()
    MEMORY_FILE.write_text(json.dumps(memories, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Remembered '{key}': {fact}"


def recall_memories() -> str:
    """Retrieve all facts stored in persistent long-term memory."""
    memories = load_memories()
    if not memories:
        return "Long-term memory is currently empty."
    lines = [f"- {k}: {v}" for k, v in memories.items()]
    return "Stored memories:\n" + "\n".join(lines)


def save_note(filename: str, content: str) -> str:
    """Save text content to a file strictly inside the agent's workspace folder."""
    try:
        target = _safe_workspace_path(filename)
        target.write_text(content, encoding="utf-8")
        return f"Saved {len(content)} characters to workspace/{target.name}"
    except Exception as exc:
        return f"Error saving note '{filename}': {exc}"


def read_note(filename: str) -> str:
    """Read text content from a file strictly inside the agent's workspace folder."""
    try:
        target = _safe_workspace_path(filename)
        if not target.exists():
            return f"File 'workspace/{target.name}' does not exist."
        return target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error reading note '{filename}': {exc}"


def list_notes() -> str:
    """List all files saved inside the agent's workspace folder."""
    files = sorted(p.name for p in WORKSPACE_DIR.iterdir() if p.is_file())
    if not files:
        return "The workspace folder is currently empty."
    return "Files in workspace: " + ", ".join(files)


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
