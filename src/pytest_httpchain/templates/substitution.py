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
    """Set simpleeval's comprehension-length cap, which it exposes only as a
    module global — so this is process-wide, and the one place that writes it."""
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

# Names an expression gets for free. The validator reads this to tell an
# undefined variable from an engine-provided name.
TEMPLATE_BUILTINS = set(SAFE_FUNCTIONS) | set(JSON_LITERALS) | {"exists", "get"} | set(DEFAULT_FUNCTIONS)


def _eval_with_context(expr: str, context: Mapping[str, Any]) -> Any:
    """Evaluate one expression; every failure becomes a `TemplatesError`."""
    # simpleeval keeps callables and data in separate maps.
    callables = {key: value for key, value in context.items() if callable(value)}
    names = {key: value for key, value in context.items() if not callable(value)}

    # exists()/get() must see the whole context, callables included, so they are
    # bound to a full copy.
    context_dict = dict(context)

    # Merge order is load-bearing: last wins, so user callables shadow the safe
    # functions while `exists`/`get` cannot be overridden, and user names shadow
    # the JSON literals.
    eval_instance = EvalWithCompoundTypes(
        functions=SAFE_FUNCTIONS
        | DEFAULT_FUNCTIONS
        | callables
        | {
            "exists": context_dict.__contains__,
            "get": context_dict.get,
        },
        names=JSON_LITERALS | names,
    )

    # Rebuilt in its original {{ … }} form: an f-string would collapse the braces
    # and show text that is not in the user's scenario.
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
        # A context callable can raise anything, and everything out of here must
        # be a TemplatesError.
        raise TemplatesError(f"Error evaluating expression '{display}': {e}") from e


def _sub_string(line: str, context: Mapping[str, Any]) -> Any:
    # A whole-string expression keeps its evaluated type; anything else is
    # interpolated into the string.
    if (expr := extract_template_expression(line)) is not None:
        return _eval_with_context(expr, context)

    def _repl(match: re.Match[str]) -> str:
        return str(_eval_with_context(match.group("expr").strip(), context))

    return re.sub(TEMPLATE_PATTERN, _repl, line)


def contains_template(obj: Any) -> bool:
    """True when any string anywhere in the structure holds a template."""
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
    """Substitute every template in a structure, returning the same shape.

    A model is dumped, substituted and re-validated (so the result is checked
    against the real field types), and returned untouched when it holds no
    template at all.
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
