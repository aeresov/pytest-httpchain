import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from pydantic import BaseModel
from simpleeval import (
    DEFAULT_FUNCTIONS,
    DEFAULT_NAMES,
    AttributeDoesNotExist,
    EvalWithCompoundTypes,
    FunctionNotDefined,
    InvalidExpression,
    IterableTooLong,
    NameNotDefined,
    NumberTooHigh,
    OperatorNotDefined,
)

from pytest_httpchain_templates.exceptions import TemplatesError
from pytest_httpchain_templates.expressions import TEMPLATE_PATTERN


def get_env(name: str, default=None):
    import os

    return os.environ.get(name, default)


SAFE_FUNCTIONS = {
    "bool": bool,
    "len": len,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "sorted": sorted,
    "enumerate": enumerate,
    "zip": zip,
    "range": range,
    "dict": dict,
    "list": list,
    "tuple": tuple,
    "set": set,
    "uuid4": lambda: str(uuid4()),
    "env": lambda var, default=None: get_env(var, default),
}

# JSON-style boolean literals (lowercase) for compatibility
JSON_LITERALS = {
    "true": True,
    "false": False,
    "null": None,
}

evaluator = EvalWithCompoundTypes(functions=SAFE_FUNCTIONS | DEFAULT_FUNCTIONS | DEFAULT_NAMES)


def _eval_with_context(expr: str, context: Mapping[str, Any]) -> Any:
    """Evaluate an expression safely using simpleeval with compound types support.

    Args:
        expr: The expression to evaluate
        context: Dictionary of variables available in the expression

    Returns:
        The evaluated result

    Raises:
        TemplatesError: If variable is not found or expression is invalid
    """
    callables = {}
    names = {}

    for key, value in context.items():
        if callable(value):
            callables[key] = value
        else:
            names[key] = value

    # Add helper functions with access to the context
    context_dict = dict(context)

    # Helper function to check if a variable exists
    def exists(var_name):
        """Check if a variable exists in the context."""
        return var_name in context_dict

    # Helper function to safely get a value with optional default
    def get(var_name, default_value=None):
        """Get a variable from context with optional default."""
        return context_dict.get(var_name, default_value)

    eval_instance = EvalWithCompoundTypes(
        functions=SAFE_FUNCTIONS
        | DEFAULT_FUNCTIONS
        | callables
        | {
            "exists": exists,
            "get": get,
        },
        names=JSON_LITERALS | names,
    )

    try:
        return eval_instance.eval(expr)
    except NameNotDefined as e:
        raise TemplatesError(f"Undefined variable in expression '{{ {expr} }}'") from e
    except FunctionNotDefined as e:
        raise TemplatesError(f"Unknown function in expression '{{ {expr} }}'") from e
    except AttributeDoesNotExist as e:
        raise TemplatesError(f"Attribute error in expression '{{ {expr} }}'") from e
    except OperatorNotDefined as e:
        raise TemplatesError(f"Operator not allowed in expression '{{ {expr} }}'") from e
    except (NumberTooHigh, IterableTooLong) as e:
        raise TemplatesError(f"Expression too complex '{{ {expr} }}'") from e
    except (InvalidExpression, SyntaxError) as e:
        raise TemplatesError(f"Invalid expression '{{ {expr} }}'") from e
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError) as e:
        error_type = type(e).__name__
        raise TemplatesError(f"{error_type} in expression '{{ {expr} }}'") from e


def _sub_string(line: str, context: Mapping[str, Any]) -> Any:
    def _repl(match: re.Match[str]) -> Any:
        expr = match.group("expr").strip()
        return _eval_with_context(expr, context)

    # Check if entire string is a single template expression
    if match := re.fullmatch(TEMPLATE_PATTERN, line):
        return _repl(match)

    # Otherwise, replace template expressions in the string
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
            return _contains_template(obj.model_dump(mode="python"))
        case SimpleNamespace():
            return any(_contains_template(value) for value in vars(obj).values())
        case _:
            return False


def walk(obj: Any, context: Mapping[str, Any]) -> Any:
    """Recursively substitute values in string attributes of an arbitrary object.

    Args:
        obj: The object to walk through (can be dict, list, str, BaseModel, SimpleNamespace, etc.)
        context: Mapping of variables for substitution (dict, ChainMap, etc.)

    Returns:
        The object with all template expressions substituted
    """
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
        case SimpleNamespace():
            if not _contains_template(obj):
                return obj

            namespace_dict = vars(obj)
            processed_dict = walk(namespace_dict, context)
            return SimpleNamespace(**processed_dict)
        case _:
            return obj
