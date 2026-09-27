"""Small helpers shared by the collection and runtime paths: markers,
substitution resolution, and scenario-relative paths.

``process_substitutions`` raises ``StageExecutionError`` even when called at
collection time (the collection caller re-wraps it into a ``CollectError``)
rather than introducing a second error type for the same malformed input.
"""

import ast
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from pytest_httpchain.errors import SchemaFileError, StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, Substitution, VarsSubstitution
from pytest_httpchain.templates import walk
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


def resolve_scenario_path(scenario_dir: Path | None, value: str | Path) -> Path:
    """Resolve a scenario-relative file path against the scenario's directory,
    matching ``$ref`` rather than the invocation CWD. Absolute paths pass
    through, as does everything when no ``scenario_dir`` is known."""
    path = Path(value)
    if path.is_absolute() or scenario_dir is None:
        return path
    return scenario_dir / path


def request_content(request: httpx.Request) -> bytes | None:
    """The request body bytes, or None when httpx never buffered them.

    A streaming body — multipart ``files`` on the real transport — is consumed
    on send without being read into ``.content``, which then raises
    ``RequestNotRead``; reporting paths must degrade, not error.
    """
    try:
        return request.content
    except httpx.RequestNotRead:
        return None


def read_json_schema_file(path: Path) -> Any:
    """Parse a referenced JSON Schema file, or raise `SchemaFileError`.

    The catch is the load-bearing part and must not be re-derived per caller:
    `JSON_PARSE_ERRORS` makes a non-UTF-8 or too deeply nested schema file fail
    cleanly instead of escaping the abort machinery as a raw traceback. The
    meta-check stays with the callers, which report an unparseable file and an
    invalid schema differently.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
        # Flattened per step, deliberately: `walk()` rebuilds its evaluator from
        # a full pass over whatever mapping it is handed, so layering a ChainMap
        # here would be re-walked once per rendered VALUE — measurably worse than
        # the single copy it would replace. Rebuilding per step is also what keeps
        # a step's own names out of its own scope.
        current_context = {**(context or {}), **result}
        match step:
            case FunctionsSubstitution():
                for alias, func_def in step.functions.items():
                    name, default_kwargs = call_target(func_def)
                    result[alias] = wrap_function(_resolve_function_name(name, current_context), default_kwargs=default_kwargs)
                    logger.debug("Seeded %s", alias)

            case VarsSubstitution():
                for key, value in step.vars.items():
                    resolved_value = walk(value, current_context)
                    result[key] = resolved_value
                    # Names only, at DEBUG: a substituted value can be an auth
                    # token, and pytest attaches captured logs to failure
                    # reports. Same boundary as the carrier's context dumps.
                    logger.debug("Seeded %s", key)

            case _:
                raise RuntimeError(f"Unhandled substitution type: {type(step).__name__}")

    return result
