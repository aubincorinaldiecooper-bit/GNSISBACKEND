"""A couple of safe, dependency-free built-in tools.

The calculator is the canonical capability for the tool-calling agent: it is
exact, deterministic, and impossible for a language model to reliably emulate
without calling it — which makes it a clean signal for the evolution loop.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

from .base import Tool, ToolResult

# Whitelisted operators — no names, calls, attributes, or comprehensions.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.USub: operator.neg, ast.UAdd: operator.pos}

_MAX_POW = 64  # guard against accidental huge exponents


def safe_arithmetic(expression: str) -> float:
    """Evaluate a pure arithmetic expression with no names or calls."""
    tree = ast.parse(expression, mode="eval")
    return _eval(tree.body)


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        if isinstance(node.op, ast.Pow):
            exponent = _eval(node.right)
            if abs(exponent) > _MAX_POW:
                raise ValueError("exponent too large")
            return _BIN_OPS[type(node.op)](_eval(node.left), exponent)
        return _BIN_OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_eval(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


class CalculatorTool(Tool):
    name = "calculator"
    description = (
        "Evaluate an arithmetic expression and return the exact numeric result. "
        "Supports + - * / // % ** and parentheses."
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "The arithmetic expression, e.g. '(12 + 30) * 2'.",
            }
        },
        "required": ["expression"],
    }

    def run(self, **kwargs: Any) -> ToolResult:
        expression = str(kwargs.get("expression", "")).strip()
        if not expression:
            return ToolResult("error: no expression provided", is_error=True)
        try:
            value = safe_arithmetic(expression)
        except Exception as exc:  # noqa: BLE001 - report any parse/eval failure to the model
            return ToolResult(f"error: could not evaluate {expression!r}: {exc}", is_error=True)
        # Render integers without a trailing ".0" so equality checks are clean.
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return ToolResult(str(value))


class StringReverseTool(Tool):
    name = "string_reverse"
    description = "Reverse a string, character by character."
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The text to reverse."}},
        "required": ["text"],
    }

    def run(self, **kwargs: Any) -> ToolResult:
        text = str(kwargs.get("text", ""))
        return ToolResult(text[::-1])
