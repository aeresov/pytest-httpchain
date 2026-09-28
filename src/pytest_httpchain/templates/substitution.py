import ast
import inspect
import os
import re
from collections.abc import Callable, Mapping
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
from pytest_httpchain.templates.functions import HELPER_FUNCTIONS


def set_max_comprehension_length(length: int) -> None:
    """Set simpleeval's comprehension-length cap, which it exposes only as a
    module global — so this is process-wide, and the one place that writes it."""
    simpleeval.MAX_COMPREHENSION_LENGTH = length  # ty: ignore[invalid-assignment]


def get_max_comprehension_length() -> int:
    """The current process-wide cap, so ``pytest_unconfigure`` can restore what
    it found (an in-process pytester run must not leak its cap to the host)."""
    return simpleeval.MAX_COMPREHENSION_LENGTH


def env(key: str, default: Any = None) -> Any:
    """``os.environ.get``, as a function of its own: the bound method's repr
    lists every environment variable with its value, so a template that got
    it uncalled (``str(env)``) sent the whole environment in its request."""
    return os.environ.get(key, default)


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
    "env": env,
    # Time, encoding, hashing and URL helpers (see functions.py).
    **HELPER_FUNCTIONS,
}

# JSON-style boolean literals (lowercase) for compatibility
JSON_LITERALS = {
    "true": True,
    "false": False,
    "null": None,
}

# The context helpers `_build_evaluator` binds to each context. It merges them
# last, so a user callable of the same name never replaces them: a call always
# reaches the built-in, which the validator's reference model relies on.
CONTEXT_HELPERS = frozenset({"exists", "get"})

# Names an expression gets for free. The validator reads this to tell an
# undefined variable from an engine-provided name.
TEMPLATE_BUILTINS = frozenset({*SAFE_FUNCTIONS, *JSON_LITERALS, *CONTEXT_HELPERS, *DEFAULT_FUNCTIONS})

# The built-ins of no use but called: the time, encoding, URL and hashing
# helpers (functions.py), uuid4, env, rand and randint. Rendered uncalled
# (``{{ now }}``), each would put its repr (``<function now at 0x...>``) in the
# request, so `_eval_expr` refuses that and the validator warns of it
# (HTTPCHAIN035). The conversions and the collection and math built-ins
# (``str``, ``len``, ``max``) are left out: a ``key=`` takes them as they are.
CALL_ONLY_BUILTINS = frozenset({*HELPER_FUNCTIONS, "uuid4", "env", "rand", "randint"})
_CALL_ONLY_FUNCTIONS = {name: function for name, function in (SAFE_FUNCTIONS | DEFAULT_FUNCTIONS).items() if name in CALL_ONLY_BUILTINS}
# By identity, which every value has: a template's value may be unhashable.
_CALL_ONLY = {id(function): name for name, function in _CALL_ONLY_FUNCTIONS.items()}


def _takes_arguments(function: Callable[..., Any]) -> bool:
    """Whether ``function`` cannot be called without arguments (so if its
    signature cannot be read, to be safe)."""
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in parameters)


_CALL_FORMS = {name: f"{name}(...)" if _takes_arguments(function) else f"{name}()" for name, function in _CALL_ONLY_FUNCTIONS.items()}


def call_form(name: str) -> str:
    """How advice writes a call of the call-only built-in ``name``: ``now()``,
    or ``env(...)`` for one that cannot be called without arguments."""
    return _CALL_FORMS[name]


def _build_evaluator(context: Mapping[str, Any]) -> EvalWithCompoundTypes:
    """Build one evaluator for a ``walker()``, which every ``walk()`` binds.

    simpleeval is meant to be built once and fed many expressions. The maps
    derive purely from ``context``, so each walker builds its own — which also
    keeps parallel iterations sharing nothing, as long as none shares a walker.
    """
    # One traversal, not three. ``context`` is a ChainMap that gains a layer per
    # stage and per save step, so every pass over it resolves each name through
    # the whole chain — and a stage repeats this once per iteration. Iterating
    # ``items()`` keeps first-layer-wins (it resolves each key through the chain,
    # so a name is classified by the value that actually wins) while filling all
    # three maps at once.
    # simpleeval keeps callables and data in separate maps; exists()/get() must
    # see the whole context, callables included, so they get a full copy.
    callables: dict[str, Any] = {}
    names: dict[str, Any] = {}
    context_dict: dict[str, Any] = {}
    for key, value in context.items():
        context_dict[key] = value
        if callable(value):
            callables[key] = value
        else:
            names[key] = value

    # Merge order is load-bearing: last wins, so user callables shadow the safe
    # functions while `exists`/`get` (CONTEXT_HELPERS) cannot be overridden, and
    # user names shadow the JSON literals.
    return EvalWithCompoundTypes(
        functions=SAFE_FUNCTIONS
        | DEFAULT_FUNCTIONS
        | callables
        | {
            "exists": context_dict.__contains__,
            "get": context_dict.get,
        },
        names=JSON_LITERALS | names,
    )


class _UncalledBuiltin(Exception):
    """An expression evaluated to a call-only built-in itself, not to its value."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def _eval_expr(evaluator: EvalWithCompoundTypes, expr: str, *, as_text: bool = False) -> Any:
    """Evaluate one expression; every failure becomes a `TemplatesError`.

    ``as_text`` returns the value's ``str()`` for interpolation. That runs in
    here because it can raise too — an int past Python's digit limit, an object
    whose ``__str__`` fails — and must fail the same way.

    A value that is a call-only built-in itself (``{{ now }}``, parentheses
    forgotten, or a save of that name that has not landed) is refused rather
    than rendered as its repr (`CALL_ONLY_BUILTINS`).
    """
    # Rebuilt in its original {{ … }} form: an f-string would collapse the braces
    # and show text that is not in the user's scenario.
    display = "{{ " + expr + " }}"
    try:
        # simpleeval parses in exec mode and, handed `a; b`, evaluates only `a`
        # behind a mere warning — so `{{ ok == True; False }}` passed a verify.
        # Parse here to refuse that, and hand simpleeval the one statement; an
        # empty parse is left to simpleeval, which refuses it itself.
        statements = ast.parse(expr.strip()).body
        if len(statements) > 1:
            raise InvalidExpression(f"a template holds one expression, not {len(statements)} statements separated by ';'")
        value = evaluator.eval(expr, statements[0] if statements else None)
        if (name := _CALL_ONLY.get(id(value))) is not None:
            raise _UncalledBuiltin(name)
        return str(value) if as_text else value
    except _UncalledBuiltin as e:
        # A built-in is what the name evaluates to only where no value of the
        # user's has that name: parentheses forgotten, or the user's own name
        # (a save that has not landed) out of scope. The message holds both.
        raise TemplatesError(
            f"Uncalled function in expression '{display}': no value named '{e.name}' is defined here, so {e.name} is the built-in function, "
            f"not a value; if the built-in is meant, call it: {call_form(e.name)}"
        ) from None
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
    except Exception as e:
        # A context callable can raise anything, and everything out of here must
        # be a TemplatesError. Naming the type is what makes the message useful,
        # so every remaining error gets it — not just a hand-picked tuple.
        raise TemplatesError(f"{type(e).__name__} in expression '{display}': {e}") from e


def _sub_string(line: str, evaluator: EvalWithCompoundTypes) -> Any:
    # A whole-string expression keeps its evaluated type; anything else is
    # interpolated into the string.
    if (expr := extract_template_expression(line)) is not None:
        return _eval_expr(evaluator, expr)

    def _repl(match: re.Match[str]) -> str:
        return _eval_expr(evaluator, match.group("expr").strip(), as_text=True)

    return re.sub(TEMPLATE_PATTERN, _repl, line)


def contains_template(obj: Any) -> bool:
    """True when any string anywhere in the structure holds a template.

    Iterative on purpose: a recursive walk spent two stack frames per level of
    nesting, so a value a few hundred levels deep overflowed it.
    """
    pending = [obj]
    while pending:
        match pending.pop():
            case str() as text:
                if re.search(TEMPLATE_PATTERN, text):
                    return True
            case dict() as mapping:
                pending.extend(mapping.values())
            case list() | tuple() as items:
                pending.extend(items)
            case BaseModel() as model:
                pending.append(model.model_dump(mode="python"))
            case SimpleNamespace() as namespace:
                pending.extend(vars(namespace).values())
    return False


def _walk(obj: Any, evaluator: EvalWithCompoundTypes) -> Any:
    match obj:
        case str():
            return _sub_string(obj, evaluator)
        case dict():
            return {key: _walk(value, evaluator) for key, value in obj.items()}
        case list():
            return [_walk(item, evaluator) for item in obj]
        case tuple():
            return tuple(_walk(item, evaluator) for item in obj)
        case BaseModel():
            if not contains_template(obj):
                return obj

            obj_dict = obj.model_dump(mode="python")
            processed_dict = _walk(obj_dict, evaluator)
            return type(obj).model_validate(processed_dict)
        case SimpleNamespace():
            if not contains_template(obj):
                return obj
            # Walked in place, not by handing vars() to the dict case: that took
            # two frames per level of a nested ``vars`` value.
            return SimpleNamespace(**{key: _walk(value, evaluator) for key, value in vars(obj).items()})
        case _:
            return obj


def _depth_guarded[T](fn: Callable[..., T], *args: Any) -> T:
    """Call ``fn``, failing a structure nested past the stack as the
    `TemplatesError` callers already report, not as a bare RecursionError."""
    try:
        return fn(*args)
    except RecursionError as e:
        raise TemplatesError(f"Value nested too deeply to substitute ({e})") from e


def walker(context: Mapping[str, Any]) -> Callable[[Any], Any]:
    """`walk` bound to one context: the evaluator is built once, here, and
    serves every call.

    For a caller that renders many values against the same context. Building
    the evaluator is a full pass over the context, so calling `walk` per value
    pays that pass once per value. The context is read now, so later changes to
    the mapping are not seen. A call that raises leaves the evaluator as it
    found it, so a caller catching each value's `TemplatesError` apart (a verify
    step rendering check by check) gets the next one substituted as though it
    came first.

    Not thread-safe: simpleeval mutates its evaluator while evaluating (a
    comprehension swaps in its own name lookup), so a thread or parallel
    iteration builds a walker of its own, never shares one.
    """
    evaluator = _depth_guarded(_build_evaluator, context)
    return lambda obj: _depth_guarded(_walk, obj, evaluator)


def walk(obj: Any, context: Mapping[str, Any]) -> Any:
    """Substitute every template in a structure, returning the same shape.

    One evaluator serves the whole traversal. A model is dumped, substituted and
    re-validated (so the result is checked against the real field types), and
    returned untouched when it holds no template at all.

    The walk recurses once per level of nesting, so a value nested deeper than
    the stack allows fails as a `TemplatesError`, which callers already report,
    rather than as a bare RecursionError.
    """
    return walker(context)(obj)
