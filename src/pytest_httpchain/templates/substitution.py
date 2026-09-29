import ast
import copy
import inspect
import os
import re
from collections.abc import Callable, Mapping
from types import MethodType, SimpleNamespace
from typing import Any
from uuid import uuid4

import simpleeval
from pydantic import BaseModel
from simpleeval import (
    DEFAULT_FUNCTIONS,
    DISALLOW_METHODS,
    DISALLOW_PREFIXES,
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


def template_form(expr: str) -> str:
    """How a message writes the template that holds ``expr``: ``{{ expr }}``,
    as the scenario writes it, with a lone surrogate (one JSON \\u escape
    away) escaped as that escape, since no UTF-8 stream can print it. The
    runtime's messages and the validator's (HTTPCHAIN037/038) name a
    template this way."""
    # Concatenated: an f-string would collapse the braces and show text that
    # is not in the user's scenario.
    return ("{{ " + expr + " }}").encode("utf-8", "backslashreplace").decode("utf-8")


class _KeyDoesNotExist(InvalidExpression):
    """A subscript's key is not in the object (or dict) it reads, as
    simpleeval's `AttributeDoesNotExist` says of an attribute."""

    def __init__(self, key: object, expression: str):
        self.message = f"Key {key!r} does not exist in expression '{expression}'"
        super(InvalidExpression, self).__init__(self.message)


class _MethodNotCalled(AttributeDoesNotExist):
    """An attribute named like a method of a ``vars`` object that has no key
    of that name, used other than as a method is (`_takes_a_method`): the
    missing attribute it was before the object had methods, whose message
    adds how the method is called."""

    def __init__(self, method: MethodType, expression: str):
        name = method.__name__
        super().__init__(name, expression)
        call = f".{name}(...)" if _takes_arguments(method) else f".{name}()"
        self.message = f"{self.message}; the object has no key '{name}'; to call its method, write {call}"
        self.args = (self.message,)


def _is_object_method(value: Any) -> bool:
    """Whether ``value`` is a method of an object a template reads by key and
    by attribute (a ``vars`` object: a SimpleNamespace that is a Mapping, the
    models' `VarsNamespace`, which this package does not know by name), which
    an attribute reaches only where the object has no key of that name. A
    fixture object's methods are left alone."""
    return isinstance(value, MethodType) and isinstance(value.__self__, SimpleNamespace) and isinstance(value.__self__, Mapping)


def _takes_a_method(tree: ast.AST, attribute: ast.Attribute) -> bool:
    """Whether ``tree`` uses ``attribute`` as a method is used: called
    (``user.get('nick')``), or handed as a call's ``key=``
    (``max(scores, key=scores.get)``), the one argument `scoping` takes for a
    function wherever it is handed."""
    return any(
        isinstance(node, ast.Call) and (node.func is attribute or any(keyword.arg == "key" and keyword.value is attribute for keyword in node.keywords)) for node in ast.walk(tree)
    )


class _Evaluator(EvalWithCompoundTypes):
    """simpleeval's evaluator, with a subscript that names a missing key and
    an attribute that reaches a ``vars`` object's method only to call it.

    simpleeval lets the KeyError of ``user['nick']`` out as it is, whose
    message is the bare key: `_eval_expr` fails it as the attribute path fails
    ``user.nick``. Only the lookup's own KeyError is caught, not one raised
    while evaluating the object or the key (a user function's).

    An attribute a ``vars`` object has no key for falls through to the
    object's method of that name, if any (``order.items`` where ``order``
    holds no ``items``). Used anywhere but called or handed as a ``key=``, it
    is refused where it is read (`_MethodNotCalled`), not only as a
    template's whole value: in a comparison, ``bool()`` or ``str()`` the
    method is a value too, always true and never equal to data, so
    ``{{ order.items != [] }}`` would pass a check that failed while the
    object had no methods, and ``str(order.keys)`` put the whole object into
    text. How the attribute is used is read off the tree being evaluated,
    only when it evaluates to such a method: the common path pays nothing,
    and nothing hangs on the order simpleeval evaluates a call's parts in."""

    # The expression `eval` is evaluating. A walker's evaluator serves one
    # thread (see `walker`), as simpleeval's own ``expr`` requires.
    _tree: ast.AST

    def eval(self, expr: str, previously_parsed: ast.AST | None = None) -> Any:
        self._tree = previously_parsed if previously_parsed is not None else self.parse(expr)
        return super().eval(expr, self._tree)

    def _eval_attribute(self, node: ast.Attribute) -> Any:
        value = super()._eval_attribute(node)
        if _is_object_method(value) and not _takes_a_method(self._tree, node):
            raise _MethodNotCalled(value, self.expr)
        return value

    def _eval_subscript(self, node: ast.Subscript) -> Any:
        container = self._eval(node.value)
        key = self._eval(node.slice)
        try:
            return container[key]
        except KeyError:
            raise _KeyDoesNotExist(key, self.expr) from None


# The expression kinds the engine evaluates: those simpleeval's evaluator
# dispatches, read off one so that they follow its version. It refuses any
# other kind ("Sorry, Lambda is not available in this evaluator"), but only
# once evaluation reaches it, so `parse_expression` refuses them up front.
# (`nodes` is typed as optional: simpleeval's __del__ clears it.)
_EVALUATED_KINDS = frozenset(_Evaluator().nodes or ())

# How a refusal names an expression kind the engine does not evaluate. Any
# other (one a later Python adds) goes by its node's name.
_UNEVALUATED_KINDS: dict[type[ast.expr], str] = {
    ast.Lambda: "a lambda",
    ast.SetComp: "a set comprehension; write set(... for ...)",
    ast.Starred: "'*' unpacking outside a list literal ([*a, *b])",
    ast.Yield: "'yield'",
    ast.YieldFrom: "'yield from'",
    ast.Await: "'await'",
}


def _refusal(node: ast.AST, spread: set[int]) -> str | None:
    """Why the engine refuses ``node`` from its text alone, if it does.

    Each is a refusal simpleeval makes once evaluation reaches the node, read
    off the same lists it reads (`simpleeval.DISALLOW_PREFIXES`,
    ``DISALLOW_METHODS``): an attribute it never reads, a call of anything but
    a name or an attribute (``fns[0]()``, which it takes for a lambda), a kind
    of expression it does not evaluate. The one kind it takes without
    dispatching it is a ``*`` element of a list literal, which the list spreads
    itself (``[*a, *b]``): ``spread`` holds those.
    """
    match node:
        case ast.NamedExpr():
            return "a template cannot assign a name (':=')"
        case ast.Attribute(attr=attr) if any(attr.startswith(prefix) for prefix in DISALLOW_PREFIXES):
            # A key of data read as an attribute (``doc._id``) can be subscripted.
            hint = "" if attr.startswith("__") else f"; for a key of that name, write [{attr!r}]"
            return f"the template engine does not read an attribute named {attr!r}{hint}"
        case ast.Attribute(attr=attr) if attr in DISALLOW_METHODS:
            hint = "; build the text with an f-string or +" if attr.startswith("format") else ""
            return f"the template engine does not read the attribute {attr!r}{hint}"
        case ast.Call(func=ast.Name() | ast.Attribute()):
            return None
        case ast.Call(func=func) if type(func) in _EVALUATED_KINDS:
            # A function of a kind not evaluated at all is refused as that.
            return "the template engine calls only a name or an attribute (f(), obj.method())"
        case ast.expr() if type(node) not in _EVALUATED_KINDS and id(node) not in spread:
            return f"the template engine does not evaluate {_UNEVALUATED_KINDS.get(type(node), type(node).__name__)}"
    return None


def _refuse_unevaluated(expression: ast.expr) -> None:
    """Raise the `TemplatesError` for the first part of ``expression`` the
    engine refuses from its text alone (`_refusal`).

    Anywhere in the tree, a branch evaluation may never take included, so
    that the validator sees the same refusal: what the engine refuses only for
    a value (an undefined name, a function it forbids) is left to it.
    """
    spread: set[int] = set()
    # Breadth first, so a list literal comes before the elements it spreads.
    for node in ast.walk(expression):
        if isinstance(node, ast.List) and isinstance(node.ctx, ast.Load):
            spread.update(id(element) for element in node.elts if isinstance(element, ast.Starred))
        if (reason := _refusal(node, spread)) is not None:
            raise TemplatesError(reason)


def parse_expression(expr: str) -> ast.expr:
    """The one expression the text of a template holds, parsed as the engine
    evaluates it, or a `TemplatesError` saying why the text is none.

    simpleeval parses in exec mode and evaluates part of what it should refuse
    behind a mere warning: ``a; b`` as ``a``, and an assignment ``x = 1`` or
    ``x += 1`` as its right-hand side, so ``{{ ok == True; False }}`` and
    ``{{ user.active = True }}`` passed a verify. Here anything but one
    expression that assigns no name, of kinds the engine evaluates, is
    refused. `_eval_expr` evaluates only what this returns, and the validator
    reads the same parse (HTTPCHAIN037/038, and `scoping`'s references), so
    what ``validate`` flags is exactly what fails when rendered.
    """
    try:
        statements = ast.parse(expr.strip()).body
    except SyntaxError as e:
        raise TemplatesError(e.msg) from e
    except (MemoryError, RecursionError) as e:
        # CPython's parser raises these for text nested past its own stack
        # (``-`` a few thousand times over, or a long ``a.b.c...`` chain).
        raise TemplatesError("the expression is too complex to parse") from e
    except UnicodeEncodeError as e:
        # The parser encodes the text as UTF-8, which a lone surrogate (one
        # JSON \u escape away) cannot be: no SyntaxError, this.
        raise TemplatesError(f"the expression holds {e.object[e.start : e.end]!r}, which is not valid text ({e.reason})") from e
    if not statements:
        raise TemplatesError("a template holds one expression, and this one is empty")
    if len(statements) > 1:
        raise TemplatesError(f"a template holds one expression, not {len(statements)} statements separated by ';'")
    match statements[0]:
        case ast.Expr(value=expression):
            pass
        case ast.Assign():
            raise TemplatesError("a template holds one expression, not an assignment; to compare two values, write '=='")
        case ast.AugAssign() | ast.AnnAssign():
            raise TemplatesError("a template holds one expression, not an assignment")
        case _:
            raise TemplatesError("a template holds one expression, not a statement")
    _refuse_unevaluated(expression)
    return expression


def _build_evaluator(context: Mapping[str, Any]) -> _Evaluator:
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
    return _Evaluator(
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


def _eval_expr(evaluator: _Evaluator, expr: str, *, as_text: bool = False) -> Any:
    """Evaluate one expression; every failure becomes a `TemplatesError`.

    ``as_text`` returns the value's ``str()`` for interpolation. That runs in
    here because it can raise too — an int past Python's digit limit, an object
    whose ``__str__`` fails — and must fail the same way.

    A value that is a call-only built-in itself (``{{ now }}``, parentheses
    forgotten, or a save of that name that has not landed) is refused rather
    than rendered as its repr (`CALL_ONLY_BUILTINS`). A ``vars`` object's
    method never gets this far uncalled: the evaluator refuses it where it is
    read (`_Evaluator`).
    """
    display = template_form(expr)
    try:
        expression = parse_expression(expr)
    except TemplatesError as e:
        # Apart from the evaluation's handlers, whose catch-all would take this
        # TemplatesError for a context callable's.
        raise TemplatesError(f"Invalid expression '{display}': {e}") from e.__cause__
    try:
        # The expression parsed, never the text: simpleeval's own parse would
        # evaluate part of what `parse_expression` refuses.
        value = evaluator.eval(expr, expression)
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
    except _KeyDoesNotExist as e:
        raise TemplatesError(f"Key error in expression '{display}': {e}") from e
    except OperatorNotDefined as e:
        raise TemplatesError(f"Operator not allowed in expression '{display}': {e}") from e
    except (NumberTooHigh, IterableTooLong) as e:
        raise TemplatesError(f"Expression too complex '{display}': {e}") from e
    except InvalidExpression as e:
        raise TemplatesError(f"Invalid expression '{display}': {e}") from e
    except Exception as e:
        # A context callable can raise anything, and everything out of here must
        # be a TemplatesError. Naming the type is what makes the message useful,
        # so every remaining error gets it — not just a hand-picked tuple.
        raise TemplatesError(f"{type(e).__name__} in expression '{display}': {e}") from e


def _sub_string(line: str, evaluator: _Evaluator) -> Any:
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


def _walk(obj: Any, evaluator: _Evaluator) -> Any:
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
            # two frames per level of a nested ``vars`` value. Rebuilt as its
            # own type, so a ``vars`` object keeps its key access (the models'
            # `VarsNamespace`, which this package does not know by name).
            return copy.replace(obj, **{key: _walk(value, evaluator) for key, value in vars(obj).items()})
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
