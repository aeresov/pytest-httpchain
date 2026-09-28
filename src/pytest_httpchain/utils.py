"""Small helpers shared by the collection and runtime paths: markers,
substitution resolution, scenario-relative paths, and the location paths
messages print.

``process_substitutions`` raises ``StageExecutionError`` even when called at
collection time (the collection caller re-wraps it into a ``CollectError``)
rather than introducing a second error type for the same malformed input.
"""

import ast
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from pytest_httpchain.errors import SchemaFileError, StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, Substitution, VarsSubstitution
from pytest_httpchain.templates import contains_template, walk, walker
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


def read_json_schema_file(path: Path) -> Any:
    """Parse a referenced JSON Schema file, or raise `SchemaFileError`.

    The catch is the load-bearing part and must not be re-derived per caller:
    `JSON_PARSE_ERRORS` makes a non-UTF-8 or too deeply nested schema file fail
    cleanly instead of escaping the abort machinery as a raw traceback.
    ``utf-8-sig`` accepts a byte-order mark, as the scenario loader does. The
    meta-check stays with the callers, which report an unparseable file and an
    invalid schema differently.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, *JSON_PARSE_ERRORS) as e:
        raise SchemaFileError(str(e)) from e


def schema_error_text(e: Exception) -> str:
    """``str(e)`` for a jsonschema error, or its bare ``message`` when that is
    too deep to render.

    jsonschema's ``__str__`` pretty-prints the failing instance in Python, a
    few frames per level, so a body or schema a few hundred levels deep raises
    ``RecursionError`` while the failure is being *described* — inside an
    ``except`` clause, where no sibling clause can catch it. ``message`` is
    rendered during validation and is always safe to read.
    """
    try:
        return str(e)
    except RecursionError:
        return getattr(e, "message", type(e).__name__)


def make_marker(mark_str: str) -> pytest.MarkDecorator:
    """Create a pytest marker from a string like 'skip(reason="foo")' or 'geofencing'."""
    tree = ast.parse(mark_str, mode="eval")
    node = tree.body

    if isinstance(node, ast.Name):
        return getattr(pytest.mark, node.id)

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [ast.literal_eval(a) for a in node.args]
        kwargs: dict[str, Any] = {}
        for kw in node.keywords:
            # literal_eval already rejects a `*args` node; a `**kwargs` entry
            # (arg None) must fail too rather than silently drop its keys.
            if kw.arg is None:
                raise ValueError(f"unsupported marker expression: {mark_str} (** unpacking)")
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        return getattr(pytest.mark, node.func.id)(*args, **kwargs)

    raise ValueError(f"unsupported marker expression: {mark_str}")


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
    if "{{" not in name:
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
        # value that holds a template (so `result` may already hold the step's
        # earlier names by then), and pays the evaluator's pass over the context
        # once per step rather than once per value. Template-free values skip
        # the walk entirely and are stored as they are: the scenario model's own
        # objects, not copies, as template-free namespace values already were.
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
                    if contains_template(value):
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
