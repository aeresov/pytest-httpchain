"""Small helpers shared by the collection and runtime paths: markers,
substitution resolution, and scenario-relative paths.

``process_substitutions`` raises ``StageExecutionError`` even when called at
collection time (the collection caller re-wraps it into a ``CollectError``)
rather than introducing a second error type for the same malformed input.
"""

import ast
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, Substitution, UserFunctionKwargs, UserFunctionName, VarsSubstitution
from pytest_httpchain.templates import walk
from pytest_httpchain.userfunc import wrap_function

logger = logging.getLogger(__name__)


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


def make_marker(mark_str: str) -> pytest.MarkDecorator:
    """Create a pytest marker from a string like 'skip(reason="foo")' or 'geofencing'."""
    tree = ast.parse(mark_str, mode="eval")
    node = tree.body

    if isinstance(node, ast.Name):
        return getattr(pytest.mark, node.id)

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [ast.literal_eval(a) for a in node.args]
        kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords if kw.arg is not None}
        return getattr(pytest.mark, node.func.id)(*args, **kwargs)

    raise ValueError(f"unsupported marker expression: {mark_str}")


def process_substitutions(
    substitutions: Sequence[Substitution],
    context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve substitution steps into a flat ``{name: value}`` dict.

    Steps resolve in order, each seeing the earlier steps' values over
    ``context``: ``functions`` seeds callable aliases, ``vars`` seeds values with
    their templates rendered.
    """
    result: dict[str, Any] = {}
    for step in substitutions:
        current_context = {**(context or {}), **result}
        match step:
            case FunctionsSubstitution():
                for alias, func_def in step.functions.items():
                    match func_def:
                        case UserFunctionName():
                            result[alias] = wrap_function(func_def.root)
                        case UserFunctionKwargs():
                            result[alias] = wrap_function(func_def.name.root, default_kwargs=func_def.kwargs)
                        case _:
                            raise StageExecutionError(f"Invalid function definition for '{alias}': expected UserFunctionName or UserFunctionKwargs")
                    logger.info(f"Seeded {alias} = {result[alias]}")

            case VarsSubstitution():
                for key, value in step.vars.items():
                    resolved_value = walk(value, current_context)
                    result[key] = resolved_value
                    logger.info(f"Seeded {key} = {resolved_value}")

            case _:
                raise RuntimeError(f"Unhandled substitution type: {type(step).__name__}")

    return result
