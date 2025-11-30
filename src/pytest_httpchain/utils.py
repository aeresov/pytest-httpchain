import logging
from collections.abc import Mapping, Sequence
from typing import Any

from pytest_httpchain_models import FunctionsSubstitution, Substitution, UserFunctionCall, UserFunctionKwargs, UserFunctionName, VarsSubstitution
from pytest_httpchain_templates import walk
from pytest_httpchain_userfunc import call_function, wrap_function

from .exceptions import StageExecutionError

logger = logging.getLogger(__name__)


def process_substitutions(
    substitutions: Sequence[Substitution],
    context: Mapping[str, Any] = {},
) -> dict[str, Any]:
    result = {}
    for step in substitutions:
        current_context = {**context, **result}
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


def call_user_function(func_call: UserFunctionCall, **extra_kwargs) -> object:
    match func_call:
        case UserFunctionName():
            return call_function(func_call.root, **extra_kwargs)
        case UserFunctionKwargs():
            merged_kwargs = {**func_call.kwargs, **extra_kwargs}
            return call_function(func_call.name.root, **merged_kwargs)
        case _:
            raise StageExecutionError(f"Invalid function call format: {func_call}")
