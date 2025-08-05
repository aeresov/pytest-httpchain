"""Template substitution functionality."""

import re
from typing import Any

from pydantic import BaseModel

from pytest_httpchain_engine.exceptions import SubstitutionError

# Pattern for matching template expressions with named groups
# This pattern is used both for validation and substitution
TEMPLATE_PATTERN = r"(?P<open>\{\{)(?P<expr>[^}]+?)(?P<close>\}\})"
TEMPLATE_REGEX = re.compile(TEMPLATE_PATTERN)

# Pattern for matching a complete template expression (for validation)
# This ensures the entire string is a template, not just contains one
COMPLETE_TEMPLATE_PATTERN = rf"^\s*{TEMPLATE_PATTERN}\s*$"
COMPLETE_TEMPLATE_REGEX = re.compile(COMPLETE_TEMPLATE_PATTERN)

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


def is_complete_template(value: str) -> bool:
    """Check if a string is a complete template expression."""
    return COMPLETE_TEMPLATE_REGEX.match(value) is not None


def extract_template_expression(value: str) -> str | None:
    """Extract the expression part from a complete template string."""
    match = COMPLETE_TEMPLATE_REGEX.match(value)
    if match:
        return match.group("expr").strip()
    return None


def _eval_with_context(expr: str, context: dict[str, Any]) -> Any:
    try:
        return eval(
            expr,
            globals={"__builtins__": _SAFE_BUILTINS},
            locals=context,
        )
    except NameError as e:
        raise SubstitutionError(f"Unsubstituted variable in '{{ {expr} }}'") from e
    except Exception as e:
        raise SubstitutionError("Invalid expression") from e


def _sub_string(line: str, context: dict[str, Any]) -> Any:
    def _repl(match: re.Match[str]) -> Any:
        expr: str = match.group("expr").strip()
        return _eval_with_context(expr, context)

    single_expr_match: re.Match[str] | None = re.fullmatch(TEMPLATE_PATTERN, line)
    if single_expr_match:
        # whole string is a substitution, use eval result directly
        return _repl(single_expr_match)
    else:
        # replace bits in string
        return re.sub(TEMPLATE_PATTERN, lambda m: str(_repl(m)), line)


def _contains_template(obj: Any) -> bool:
    """Check if an object contains any template strings."""
    match obj:
        case str():
            return bool(re.search(TEMPLATE_PATTERN, obj))
        case dict():
            return any(_contains_template(value) for value in obj.values())
        case list():
            return any(_contains_template(item) for item in obj)
        case BaseModel():
            obj_dict = obj.model_dump(mode="python")
            return _contains_template(obj_dict)
        case _:
            return False


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
            if not _contains_template(obj):
                return obj

            obj_dict = obj.model_dump(mode="python")
            processed_dict = walk(obj_dict, context)
            return obj.__class__.model_validate(processed_dict)
        case _:
            return obj
