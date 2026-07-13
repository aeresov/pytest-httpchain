"""Shared helpers for building pytest markers, resolving substitutions, and
invoking user functions.

These helpers are used from both the collection path (``carrier.create_test_class``
and ``plugin.JsonModule.collect`` resolve scenario-level substitutions and markers
while building the test class) and the runtime path (``Carrier.execute_stage``
resolves stage-level substitutions and calls user functions during a request).

Naming oddity: ``process_substitutions`` and ``call_user_function`` raise
``StageExecutionError`` on a malformed function definition/call, but
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
from pytest_httpchain_templates import walk
from pytest_httpchain_userfunc import call_function, wrap_function

from pytest_httpchain.models import FunctionsSubstitution, Substitution, UserFunctionCall, UserFunctionKwargs, UserFunctionName, VarsSubstitution

from .errors import StageExecutionError

logger = logging.getLogger(__name__)


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

    return result


def call_user_function(func_call: UserFunctionCall, **extra_kwargs: Any) -> object:
    """Import and call a user function described by a ``UserFunctionCall`` model.

    A bare ``UserFunctionName`` is called with only ``extra_kwargs``; a
    ``UserFunctionKwargs`` merges its declared kwargs under ``extra_kwargs``
    (caller-supplied values win on conflict). Used both for request/scenario auth
    callables and for verify/save user functions, where ``extra_kwargs`` carries
    the ``response``. Raises ``StageExecutionError`` if ``func_call`` is neither
    supported shape.
    """
    match func_call:
        case UserFunctionName():
            return call_function(func_call.root, **extra_kwargs)
        case UserFunctionKwargs():
            merged_kwargs = {**func_call.kwargs, **extra_kwargs}
            return call_function(func_call.name.root, **merged_kwargs)
        case _:
            raise StageExecutionError(f"Invalid function call format: {func_call}")
