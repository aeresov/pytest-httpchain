import re
from typing import Any

from pydantic import BaseModel

# Constants for variable substitution
_EVAL_PATTERN = r"(?P<open>\{\{)(?P<expr>[^}]+?)(?P<close>\}\})"

# Safe built-ins for eval context - following established security patterns
# Based on simpleeval and RestrictedPython safe builtins
_SAFE_BUILTINS = {
    # Type conversion functions
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    # Collection functions
    "len": len,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    # Math functions
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    # Iteration functions
    "sorted": sorted,
    "reversed": reversed,
    "enumerate": enumerate,
    "zip": zip,
    "range": range,
}


class SubstitutionError(Exception):
    """An error substituting variables."""


def _eval_with_context(expr: str, context: dict[str, Any]) -> Any:
    try:
        return eval(
            expr,
            globals={"__builtins__": _SAFE_BUILTINS},
            locals=context,
        )
    except Exception as e:
        raise SubstitutionError("Invalid expression") from e


def _sub_string(line: str, context: dict[str, Any]) -> Any:
    def _repl(match: re.Match[str]) -> Any:
        expr: str = match.group("expr").strip()
        return _eval_with_context(expr, context)

    single_expr_match: re.Match[str] | None = re.fullmatch(_EVAL_PATTERN, line)
    if single_expr_match:
        # whole string is a substitution, use eval result directly
        return _repl(single_expr_match)
    else:
        # replace bits in string
        return re.sub(_EVAL_PATTERN, lambda m: str(_repl(m)), line)


def walk(obj: Any, context: dict[str, Any]) -> Any:
    """Recursively substitute values in string attributes of an arbitrary object."""
    match obj:
        case str():
            return _sub_string(obj, context)
        case dict():
            return {key: walk(value, context) for key, value in obj.items()}
        case list():
            return [walk(item, context) for item in obj]
        case BaseModel():
            obj_dict = obj.model_dump()
            processed_dict = walk(obj_dict, context)
            return obj.__class__.model_validate(processed_dict)
        case _:
            return obj
