"""
tools.py - Tools that the AI Agent can call to interact with the world.

To add a new tool:
1. Write a Python function below.
2. Register its schema in TOOL_SCHEMAS and map its name in AVAILABLE_TOOLS.
"""

import ast
import datetime
import math
import operator
from pathlib import Path
from typing import Any, Callable, Dict, List
import requests

WORKSPACE_DIR = Path(__file__).parent / "workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# 1. Tool Implementations
# ---------------------------------------------------------------------------

def calculator(expression: str) -> str:
    """Safely evaluate a mathematical expression."""
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
        ast.Pow: operator.pow,
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
        url = f"https://wttr.in/{ requests.utils.quote(city) }?format=j1"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "first-agent/1.0"})
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


def save_note(filename: str, content: str) -> str:
    """Save text content to a file inside the agent's workspace folder."""
    safe_name = Path(filename).name
    target = WORKSPACE_DIR / safe_name
    target.write_text(content, encoding="utf-8")
    return f"Saved {len(content)} characters to workspace/{safe_name}"


def read_note(filename: str) -> str:
    """Read text content from a file inside the agent's workspace folder."""
    safe_name = Path(filename).name
    target = WORKSPACE_DIR / safe_name
    if not target.exists():
        return f"File 'workspace/{safe_name}' does not exist."
    return target.read_text(encoding="utf-8")


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
