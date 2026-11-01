"""
tools.py - The tool registry: what agents can call, and the schema each one exposes to the model.

Implementations live in swarm_tools/, one module per concern. Security controls:
1. Calculator: AST whitelist with limits on exponents and factorials (`swarm_tools/calculator.py`).
2. `fetch_url`: blocks localhost, private addresses, and cloud metadata endpoints, with the resolved
   address pinned for the request (`swarm_tools/safety.py`, `swarm_tools/web.py`).
3. File tools: confined to the tenant's workspace (`swarm_tools/safety.py`).
4. `run_python`: isolation comes from a container (`--network none`, read-only root, no capabilities),
   and it fails closed without one. `UNSAFE_ALLOW_LOCAL_EXEC=true` is a developer opt-in that runs code
   on the host behind a guardrail, not a sandbox: code in the same interpreter can undo it. Never enable
   it where untrusted input can reach `run_python` (`swarm_tools/python_exec.py`).
"""

from typing import Any, Callable, Dict, List

from swarm_tools.calculator import calculator
from swarm_tools.python_exec import run_python
from swarm_tools.self_heal import scorecard_diagnose, self_heal_codebase
from swarm_tools.web import fetch_url, get_current_time, get_weather, web_search
from swarm_tools.workspace import list_notes, read_note, recall_memories, remember_fact, save_note

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
    "scorecard_diagnose": scorecard_diagnose,
    "self_heal_codebase": self_heal_codebase,
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
            "description": (
                "Fetch a public HTTP/HTTPS URL and return status code, response headers, "
                "structural HTML/DOM metadata (<meta>, <form>, <input>, <script src>, <link>, <iframe>, <html> attrs), "
                "and readable text. Pass include_raw=True when you need the raw unstripped HTML/source snippet."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full public HTTP/HTTPS URL to inspect.",
                    },
                    "include_raw": {
                        "type": "boolean",
                        "description": "Set true to include the raw unstripped HTML/response body snippet.",
                    },
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
    {
        "type": "function",
        "function": {
            "name": "scorecard_diagnose",
            "description": "Diagnose the system's performance, code quality, token economy, and reliability using the 4-Pillar Scorecard Engine.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mission_id": {
                        "type": "string",
                        "description": "Optional mission ID to evaluate. Defaults to the latest completed mission.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "self_heal_codebase",
            "description": "Autonomously diagnose scorecard findings, propose code fixes, repair test failures, verify via isolated benchmark mission, and commit strictly winning improvements.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_fixes": {
                        "type": "integer",
                        "description": "Maximum number of findings to fix in this cycle (1-5, default 3).",
                    },
                    "verify": {
                        "type": "boolean",
                        "description": "Whether to re-run the benchmark mission in a fresh subprocess to measure real performance lift (default true).",
                    },
                    "rules": {
                        "type": "string",
                        "description": "Optional comma-separated list of rule IDs to target (e.g. 'G-B1,G-D1').",
                    },
                },
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
