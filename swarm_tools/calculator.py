"""swarm_tools/calculator.py - AST-whitelisted arithmetic, with limits against expensive inputs."""

import ast
import math
import operator
from typing import Any, Callable, Dict

from core.config import CONFIG


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
