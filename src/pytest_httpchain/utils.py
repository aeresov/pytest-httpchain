"""Small helpers shared by the collection and runtime paths, the validator
and the importers: marks and the xdist groups they declare, substitution
resolution, scenario-relative paths, the location paths messages print, a
header check's optional value as a list (`optional_as_list`), a request's
body as the reports capture it (`request_content`), what parsing JSON raises
on bad input (`JSON_PARSE_ERRORS`), and, for an exception user code raised,
whether it stops the run (`INTERRUPTS`, re-exported from errors) and how it
is described (`exception_text`, `describe_exception`).

``process_substitutions`` raises ``StageExecutionError`` even when called at
collection time (the collection caller re-wraps it into a ``CollectError``)
rather than introducing a second error type for the same malformed input.
"""

import ast
import json
import logging
import re
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

# Re-exported: the carrier, connections and the plugin import it from here,
# beside `describe_exception`.
from pytest_httpchain.errors import INTERRUPTS as INTERRUPTS
from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, Substitution, VarsSubstitution
from pytest_httpchain.templates import needs_rendering, walk, walker
from pytest_httpchain.userfunc import call_target, wrap_function

logger = logging.getLogger(__name__)

# What parsing JSON raises on bad *input*, as opposed to a bug. ``ValueError``
# subsumes ``json.JSONDecodeError`` and ``UnicodeDecodeError`` (undecodable
# bytes, which ``httpx.Response.json()`` raises too). ``RecursionError`` is what
# CPython's decoder raises on deeply nested input (``[`` * 100_000), and it is
# not a ValueError: left out, it escapes the chain-abort machinery as a raw
# traceback, with no report section and no HAR entry.
JSON_PARSE_ERRORS = (ValueError, RecursionError)


def optional_as_list(value: Any) -> list[Any]:
    """None -> [], anything else -> [value]: adapts HeaderMatcher's optional
    single-value fields to the list-based shared checks."""
    return [] if value is None else [value]


# A dict key that reads as one path segment. Others ("data.id", a JMESPath
# expression in `verify.jmespath`) are bracketed and quoted, so the path stays
# unambiguous: verify.jmespath["data.id"].eq.
_PLAIN_KEY = re.compile(r"[\w-]+")


def path_segment(key: str | int) -> str:
    """One step of a location path as the validator and the runtime print it:
    ``[0]`` for a list index, ``.name`` for a key that reads as one segment,
    and ``["data.id"]`` for any other key."""
    if isinstance(key, int):
        return f"[{key}]"
    return f".{key}" if _PLAIN_KEY.fullmatch(key) else f"[{json.dumps(key)}]"


def resolve_scenario_path(scenario_dir: Path | None, value: str | Path) -> Path:
    """Resolve a scenario-relative file path against the scenario's directory,
    matching ``$ref`` rather than the invocation CWD. Absolute paths pass
    through, as does everything when no ``scenario_dir`` is known."""
    path = Path(value)
    if path.is_absolute() or scenario_dir is None:
        return path
    return scenario_dir / path


def request_content(request: httpx.Request) -> bytes | None:
    """The request body bytes, or None for a body this does not capture.

    ``.content`` raises ``RequestNotRead`` for any request httpx did not buffer.
    A redirect follow-up that keeps its method is one: httpx builds it with the
    original's ``stream=`` and never reads it. A ``ByteStream`` is plain bytes,
    replayable, so reading it consumes nothing — the same rule httpx applies
    when it buffers a ``content=`` body.

    Any other stream is deliberately not iterated a second time: an iterator
    body would be found exhausted, a file-backed multipart body would re-read
    its files. The plugin sends none: its multipart bodies (``files`` and
    ``multipart``) are encoded to bytes before they are sent
    (`request_builder`), so they are captured as any other body is. Reporting
    paths must degrade, not error.
    """
    try:
        return request.content
    except httpx.RequestNotRead:
        if isinstance(request.stream, httpx.ByteStream):
            return request.read()
        return None


def _mark_argument(node: ast.expr, mark_str: str) -> Any:
    """The value of one argument of a mark, which is a literal's.

    One that is none (a name, a comparison, a `*args` node) is refused in the
    words the scenario wrote. ast.literal_eval's own error names the node as
    Python prints it (``<ast.Compare object at 0x7f...>``): no word of the
    mark, and an address that differs on every run."""
    try:
        return ast.literal_eval(node)
    except ValueError:
        raise ValueError(
            f"unsupported marker expression: {mark_str} ({ast.unparse(node)} is no literal: a mark's arguments are literals, a condition written as a string)"
        ) from None


def make_marker(mark_str: str) -> pytest.MarkDecorator:
    """Create a pytest marker from a string like 'skip(reason="foo")' or 'geofencing'."""
    tree = ast.parse(mark_str, mode="eval")
    node = tree.body

    if isinstance(node, ast.Name):
        return getattr(pytest.mark, node.id)

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_mark_argument(a, mark_str) for a in node.args]
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            # A `*args` node is no literal, so it is refused already; a
            # `**kwargs` entry (arg None) must fail too rather than silently
            # drop its keys.
            if kw.arg is None:
                raise ValueError(f"unsupported marker expression: {mark_str} (** unpacking)")
            kwargs[kw.arg] = _mark_argument(kw.value, mark_str)
        return getattr(pytest.mark, node.func.id)(*args, **kwargs)

    raise ValueError(f"unsupported marker expression: {mark_str}")


def parse_marker(mark_str: str) -> pytest.MarkDecorator | BaseException:
    """`make_marker`, for a check that reads a mark and reports one that does
    not parse: the mark, or what parsing it raised, returned.

    Constructing an unregistered mark emits PytestUnknownMarkWarning, which is
    noise there, so warnings are ignored while it parses. What it raises can
    be anything a parse raises (pytest's MarkGenerator raises AttributeError
    for a reserved name, ``_foo``, ast.literal_eval TypeError for exotic
    argument nodes) and, where pytest has a session's config (collection),
    pytest's Failed, which is no Exception: for a mark the session does not
    register under strict markers, and for a misspelt ``parametrize``
    (``parameterize``)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return make_marker(mark_str)
    except (Exception, pytest.fail.Exception) as e:
        return e


def xdist_group_names(markers: Iterable[pytest.MarkDecorator]) -> set[str]:
    """The pytest-xdist group names these marks declare, read as xdist reads them:
    the first argument, else the ``name`` keyword, else ``"default"``.

    xdist joins every group name on a test, the scenario's and the stage's own,
    into the one group the test is scheduled by.
    """
    return {str(marker.args[0] if marker.args else marker.kwargs.get("name", "default")) for marker in markers if marker.name == "xdist_group"}


def _resolve_function_name(name: str, context: Mapping[str, Any]) -> str:
    """Render a templated import name (``mod.{{ x }}:fn``) against the current
    context; literal names pass through untouched. The model advertises the
    template form, so it must resolve here — nothing downstream sees a context."""
    if not needs_rendering(name):
        return name
    resolved = walk(name, context)
    if not isinstance(resolved, str):
        raise StageExecutionError(f"Templated function name {name!r} must resolve to a string, got {type(resolved).__name__}")
    return resolved


def process_substitutions(
    substitutions: Sequence[Substitution],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve substitution steps into a flat ``{name: value}`` dict.

    Steps resolve in order, each seeing the earlier steps' values over
    ``context``: ``functions`` seeds callable aliases (their import names
    rendered, their kwargs passed raw), ``vars`` seeds values with their
    templates rendered.
    """
    result: dict[str, Any] = {}
    for step in substitutions:
        # Flattened per step, deliberately: a snapshot taken before the step
        # seeds anything, which keeps a step's own names out of its own scope.
        # A vars step renders through one `walker()` over it, built at the first
        # value that holds a template or an escape (so `result` may already hold
        # the step's earlier names by then), and pays the evaluator's pass over
        # the context once per step rather than once per value. Values with
        # nothing to render skip the walk entirely and are stored as they are:
        # the scenario model's own objects, not copies, as such namespace values
        # already were.
        current_context = {**(context or {}), **result}
        match step:
            case FunctionsSubstitution():
                for alias, func_def in step.functions.items():
                    name, default_kwargs = call_target(func_def)
                    result[alias] = wrap_function(_resolve_function_name(name, current_context), default_kwargs=default_kwargs)
                    logger.debug("Seeded %s", alias)

            case VarsSubstitution():
                render: Callable[[Any], Any] | None = None
                for key, value in step.vars.items():
                    if needs_rendering(value):
                        render = render or walker(current_context)
                        value = render(value)
                    result[key] = value
                    # Names only, at DEBUG: a substituted value can be an auth
                    # token, and pytest attaches captured logs to failure
                    # reports. Same boundary as the carrier's context dumps.
                    logger.debug("Seeded %s", key)

            case _:
                raise RuntimeError(f"Unhandled substitution type: {type(step).__name__}")

    return result


def exception_text(e: BaseException) -> str:
    """``str(e)`` for an exception user code raised (a fixture's context
    manager, a connection adapter), whose ``__str__`` may raise in turn: a
    placeholder then, as formatting it must not abandon what is still to be
    done."""
    try:
        return str(e)
    except Exception:
        return "<exception str() failed>"


def describe_exception(e: BaseException) -> str:
    """``Type: message`` for an exception user code raised, its message
    `exception_text`'s. One without a message is its type alone."""
    text = exception_text(e)
    return f"{type(e).__name__}: {text}" if text else type(e).__name__
