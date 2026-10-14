"""Small JSON expression interpreter; never evals source code or performs I/O."""

from decimal import Decimal, InvalidOperation
from functools import reduce
from operator import mul

from .errors import ExecutionError


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ExecutionError(f"Expected a number, got {type(value).__name__}")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ExecutionError("Non-finite arithmetic")
    return result


def _json_number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def evaluate(value, context):
    if isinstance(value, str) and value.startswith("$"):
        cursor = context
        for part in value[1:].split("."):
            if not isinstance(cursor, dict) or part not in cursor:
                raise ExecutionError(f"Unbound reference: {value}")
            cursor = cursor[part]
        return cursor
    if isinstance(value, list):
        return [evaluate(item, context) for item in value]
    if not isinstance(value, dict):
        return value
    if "op" not in value:
        return {key: evaluate(item, context) for key, item in value.items()}
    op = value["op"]
    args = [evaluate(arg, context) for arg in value.get("args", [])]
    try:
        if op in {"add", "sub", "mul", "min", "max"}:
            numbers = [_number(arg) for arg in args]
            if not numbers:
                raise ExecutionError(f"{op} needs numeric arguments")
            result = {
                "add": lambda: sum(numbers, Decimal(0)),
                "sub": lambda: numbers[0] - sum(numbers[1:], Decimal(0)),
                "mul": lambda: reduce(mul, numbers, Decimal(1)),
                "min": lambda: min(numbers),
                "max": lambda: max(numbers),
            }[op]()
            return _json_number(result)
        if op in {"eq", "ne", "lt", "lte", "gt", "gte"}:
            left, right = args
            return {
                "eq": lambda: left == right,
                "ne": lambda: left != right,
                "lt": lambda: left < right,
                "lte": lambda: left <= right,
                "gt": lambda: left > right,
                "gte": lambda: left >= right,
            }[op]()
        if op == "and":
            return all(args)
        if op == "or":
            return any(args)
        if op == "not":
            (arg,) = args
            return not arg
        if op == "contains":
            container, item = args
            return item in container
        if op == "sum":
            rows, field = args
            return _json_number(sum((_number(row[field]) for row in rows), Decimal(0)))
        if op == "count":
            (rows,) = args
            return len(rows)
        if op == "first":
            (rows,) = args
            return rows[0] if rows else None
        if op == "lookup":
            rows, key, sought, field = args
            matches = [row for row in rows if row[key] == sought]
            if len(matches) != 1:
                raise ExecutionError(f"lookup expected one {key}={sought!r}, found {len(matches)}")
            return matches[0][field]
    except (TypeError, ValueError, KeyError, IndexError, InvalidOperation) as exc:
        raise ExecutionError(f"Invalid {op} expression: {exc}") from exc
    raise ExecutionError(f"Unsupported expression operator: {op}")
