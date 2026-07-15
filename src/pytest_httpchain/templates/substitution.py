import os
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import simpleeval
from pydantic import BaseModel
from simpleeval import (
    DEFAULT_FUNCTIONS,
    AttributeDoesNotExist,
    EvalWithCompoundTypes,
    FunctionNotDefined,
    InvalidExpression,
    IterableTooLong,
    NameNotDefined,
    NumberTooHigh,
    OperatorNotDefined,
)

from pytest_httpchain.templates.exceptions import TemplatesError
from pytest_httpchain.templates.expressions import TEMPLATE_PATTERN, extract_template_expression


def set_max_comprehension_length(length: int) -> None:
    """Configure simpleeval's comprehension-length cap.

    simpleeval exposes the cap only as a module global; this is the one
    sanctioned place that mutates it, so consumers (the plugin's ini option)
    do not reach into a third-party module this package owns. Process-wide by
    nature — it affects every simpleeval user in the process.
    """
    simpleeval.MAX_COMPREHENSION_LENGTH = length  # ty: ignore[invalid-assignment]


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
    "env": os.environ.get,
}

# JSON-style boolean literals (lowercase) for compatibility
JSON_LITERALS = {
    "true": True,
    "false": False,
    "null": None,
}

# Names available inside an expression without the user defining them: the safe
# builtins, JSON literals, the context helpers added at eval time, and
# simpleeval's own defaults (int/float/str/rand/randint). The validator imports
# this to tell a genuinely undefined variable from an engine-provided name.
TEMPLATE_BUILTINS = set(SAFE_FUNCTIONS) | set(JSON_LITERALS) | {"exists", "get"} | set(DEFAULT_FUNCTIONS)


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
    # simpleeval keeps callables and data in two separate maps (functions= vs
    # names=), so the context is partitioned by callable(): a callable (user
    # function / factory fixture) goes to functions=, everything else to names=.
    callables = {}
    names = {}

    for key, value in context.items():
        if callable(value):
            callables[key] = value
        else:
            names[key] = value

    # exists()/get() must see the WHOLE context (callables included), not just the
    # `names` half — so they close over a full copy, kept in sync with the split above.
    context_dict = dict(context)

    # Helper function to check if a variable exists
    def exists(var_name):
        """Check if a variable exists in the context."""
        return var_name in context_dict

    # Helper function to safely get a value with optional default
    def get(var_name, default_value=None):
        """Get a variable from context with optional default."""
        return context_dict.get(var_name, default_value)

    # Merge order is load-bearing: on a name collision the LAST mapping wins, so
    # user-supplied `callables` can shadow SAFE_FUNCTIONS/DEFAULT_FUNCTIONS, but the
    # engine's own `exists`/`get` are merged last and therefore cannot be overridden
    # by a context value named "exists"/"get". Likewise user `names` override the
    # JSON literals. Reordering these `|` operands changes which value wins.
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

    # Render the expression back in its original {{ … }} form for error messages
    # (an f-string would otherwise collapse {{ }} to single braces, showing text
    # that does not appear in the user's scenario).
    display = "{{ " + expr + " }}"
    try:
        return eval_instance.eval(expr)
    except NameNotDefined as e:
        raise TemplatesError(f"Undefined variable in expression '{display}': {e}") from e
    except FunctionNotDefined as e:
        raise TemplatesError(f"Unknown function in expression '{display}': {e}") from e
    except AttributeDoesNotExist as e:
        raise TemplatesError(f"Attribute error in expression '{display}': {e}") from e
    except OperatorNotDefined as e:
        raise TemplatesError(f"Operator not allowed in expression '{display}': {e}") from e
    except (NumberTooHigh, IterableTooLong) as e:
        raise TemplatesError(f"Expression too complex '{display}': {e}") from e
    except (InvalidExpression, SyntaxError) as e:
        raise TemplatesError(f"Invalid expression '{display}': {e}") from e
    except (ValueError, TypeError, KeyError, IndexError, ZeroDivisionError) as e:
        error_type = type(e).__name__
        raise TemplatesError(f"{error_type} in expression '{display}': {e}") from e
    except Exception as e:
        # Terminal catch-all: anything a context callable (user function or
        # factory fixture invoked inside the expression) raises — including
        # UserFunctionError or arbitrary exceptions — would otherwise escape the
        # enumerated cases above as a raw traceback, breaking the
        # all-errors-are-TemplatesError contract.
        raise TemplatesError(f"Error evaluating expression '{display}': {e}") from e


def _sub_string(line: str, context: Mapping[str, Any]) -> Any:
    # Whole string is a single template expression (surrounding whitespace
    # allowed) — uses the same predicate the models apply when typing a field
    # as TemplateExpression, so type preservation is consistent between schema
    # validation and runtime evaluation.
    if (expr := extract_template_expression(line)) is not None:
        return _eval_with_context(expr, context)

    # Otherwise, interpolate embedded template expressions into the string.
    def _repl(match: re.Match[str]) -> str:
        return str(_eval_with_context(match.group("expr").strip(), context))

    return re.sub(TEMPLATE_PATTERN, _repl, line)


def contains_template(obj: Any) -> bool:
    """Check if an object contains any template strings."""
    match obj:
        case str():
            return bool(re.search(TEMPLATE_PATTERN, obj))
        case dict():
            return any(contains_template(value) for value in obj.values())
        case list() | tuple():
            return any(contains_template(item) for item in obj)
        case BaseModel():
            return contains_template(obj.model_dump(mode="python"))
        case SimpleNamespace():
            return any(contains_template(value) for value in vars(obj).values())
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
        case tuple():
            return tuple(walk(item, context) for item in obj)
        case BaseModel():
            if not contains_template(obj):
                return obj

            obj_dict = obj.model_dump(mode="python")
            processed_dict = walk(obj_dict, context)
            return obj.__class__.model_validate(processed_dict)
        case SimpleNamespace():
            if not contains_template(obj):
                return obj

            namespace_dict = vars(obj)
            processed_dict = walk(namespace_dict, context)
            return SimpleNamespace(**processed_dict)
        case _:
            return obj
