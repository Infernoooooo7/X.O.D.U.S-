"""Sample MCP server for XODUS: a calculator (stdio transport, Model Context Protocol, standard library only).

Tools:
  evaluate  - evaluate an arithmetic expression safely (parsed into a syntax tree and walked; never eval()).
  describe  - count, mean, median, standard deviation, minimum and maximum of a list of numbers.

It implements the server side of initialize, tools/list and tools/call, answers ping, and refuses everything else.
It reads no files and opens no network connections.
"""

import ast
import json
import math
import operator
import statistics
import sys

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_EXPRESSION = 200
MAX_DEPTH = 30
MAX_EXPONENT = 100
MAX_MAGNITUDE = 1e100

EVALUATE_SCHEMA = {"type": "object", "required": ["expression"], "properties": {
    "expression": {"type": "string", "minLength": 1, "maxLength": MAX_EXPRESSION,
                   "description": "For example: (17.5 * 4) / 3 + sqrt(2)"}}}
DESCRIBE_SCHEMA = {"type": "object", "required": ["numbers"], "properties": {
    "numbers": {"type": "array", "minItems": 1, "maxItems": 10000, "items": {"type": "number"}}}}
TOOLS = [
    {"name": "evaluate", "description": "Evaluate an arithmetic expression.", "inputSchema": EVALUATE_SCHEMA},
    {"name": "describe", "description": "Describe a list of numbers.", "inputSchema": DESCRIBE_SCHEMA},
]

BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
          ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
FUNCTIONS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "sin": math.sin, "cos": math.cos,
             "tan": math.tan, "abs": abs, "round": round, "min": min, "max": max}
CONSTANTS = {"pi": math.pi, "e": math.e}


class CalcError(Exception):
    pass


def _check(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalcError("only numbers are allowed")
    if isinstance(value, float) and not math.isfinite(value):
        raise CalcError("the result is not a finite number")
    if abs(value) > MAX_MAGNITUDE:
        raise CalcError("the result is too large")
    return value


def _eval(node, depth=0):
    if depth > MAX_DEPTH:
        raise CalcError("the expression is nested too deeply")
    if isinstance(node, ast.Expression):
        return _eval(node.body, depth + 1)
    if isinstance(node, ast.Constant):
        return _check(node.value)
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in UNARY:
        return _check(UNARY[type(node.op)](_eval(node.operand, depth + 1)))
    if isinstance(node, ast.BinOp) and type(node.op) in BINARY:
        left, right = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise CalcError(f"exponents are limited to {MAX_EXPONENT}")
        try:
            return _check(BINARY[type(node.op)](left, right))
        except ZeroDivisionError:
            raise CalcError("division by zero") from None
        except OverflowError:
            raise CalcError("the result is too large") from None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
            and not node.keywords and 1 <= len(node.args) <= 10):
        args = [_eval(a, depth + 1) for a in node.args]
        try:
            return _check(FUNCTIONS[node.func.id](*args))
        except (ValueError, TypeError) as exc:
            raise CalcError(f"{node.func.id}: {exc}") from None
    raise CalcError("only numbers, + - * / // % **, parentheses, pi, e and "
                    + ", ".join(sorted(FUNCTIONS)) + " are allowed")


def evaluate(expression):
    if len(expression) > MAX_EXPRESSION:
        raise CalcError(f"expressions are limited to {MAX_EXPRESSION} characters")
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except SyntaxError:
        raise CalcError("the expression is not valid arithmetic") from None
    value = _eval(tree)
    return {"expression": expression, "value": value}


def describe(numbers):
    values = [_check(n) for n in numbers]
    return {"count": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
            "stdev": statistics.stdev(values) if len(values) > 1 else None, "min": min(values), "max": max(values)}


def send(message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", **message}) + "\n")
    sys.stdout.flush()


def call_tool(name, arguments):
    try:
        if name == "evaluate":
            result = evaluate(str(arguments.get("expression", "")))
            text = f"{result['expression']} = {result['value']}"
        elif name == "describe":
            result = describe(list(arguments.get("numbers", [])))
            text = json.dumps(result)
        else:
            return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name!r}"}]}
    except CalcError as exc:
        return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
    return {"structuredContent": result, "content": [{"type": "text", "text": text}]}


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            send({"id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # a notification (e.g. notifications/initialized): nothing to answer
            continue
        if method == "initialize":
            asked = msg.get("params", {}).get("protocolVersion")
            send({"id": mid, "result": {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                        "capabilities": {"tools": {"listChanged": False}},
                                        "serverInfo": {"name": "xodus-calc", "version": "1.0.0"}}})
        elif method == "ping":
            send({"id": mid, "result": {}})
        elif method == "tools/list":
            send({"id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            send({"id": mid, "result": call_tool(params.get("name"), params.get("arguments") or {})})
        else:
            send({"id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
