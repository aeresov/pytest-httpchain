"""Shared helpers for building pytest markers, resolving substitutions, and
invoking user functions.

These helpers are used from both the collection path (``factory.create_test_class``
and ``plugin.JsonModule.collect`` resolve scenario-level substitutions and markers
while building the test class) and the runtime path (``Carrier.execute_stage``
resolves stage-level substitutions during a request).

Naming oddity: ``process_substitutions`` raises
``StageExecutionError`` on a malformed function definition, but
``process_substitutions(scenario.substitutions)`` is invoked at *collection* time
by ``create_test_class``. So a ``StageExecutionError`` can surface before any
stage runs; the collection caller catches it and re-wraps it into a pytest
``CollectError``. The exception name is kept for consistency with the runtime
path rather than introducing a second error type for the same malformed input.
"""

import ast
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from pytest_httpchain.errors import StageExecutionError
from pytest_httpchain.models import FunctionsSubstitution, Substitution, UserFunctionKwargs, UserFunctionName, VarsSubstitution
from pytest_httpchain.templates import walk
from pytest_httpchain.userfunc import wrap_function

logger = logging.getLogger(__name__)


def optional_as_list(value: Any) -> list[Any]:
    """None -> [], anything else -> [value]. Adapts HeaderMatcher's optional
    single-value fields to list-based shared checks (the carrier's matcher
    checks and the validator's contradiction checks share this adapter)."""
    return [] if value is None else [value]


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
    """Resolve a list of substitution steps into a flat ``{name: value}`` dict.

    Steps are processed in order and each step sees the values produced by the
    earlier steps layered over ``context`` (later steps may reference earlier
    ones). ``FunctionsSubstitution`` seeds callable aliases (wrapped user
    functions, optionally with default kwargs); ``VarsSubstitution`` seeds plain
    values, rendering any ``{{ }}`` templates against the running context.

    Raises ``StageExecutionError`` on a malformed function definition — note this
    runs at collection time when resolving scenario-level substitutions (see the
    module docstring).
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
                # New substitution variant not handled here: a plugin bug — fail
                # loudly instead of silently seeding nothing.
                raise RuntimeError(f"Unhandled substitution type: {type(step).__name__}")

    return result
