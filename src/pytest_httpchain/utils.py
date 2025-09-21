import logging
from typing import Any

import pytest_httpchain_templates.substitution
from pytest_httpchain_models.entities import Substitution, UserFunctionKwargs, UserFunctionName
from pytest_httpchain_userfunc import call_function, wrap_function

from .exceptions import StageExecutionError

logger = logging.getLogger(__name__)


def process_substitutions(
    substitutions: list[Substitution],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Process a list of substitution steps to build a context dictionary.

    This function handles both function definitions and variable assignments
    from Substitution models, evaluating them in order and building up
    a context dictionary.

    Args:
        substitutions: List of Substitution models to process
        context: Initial context for variable resolution

    Returns:
        Dictionary containing all processed functions and resolved variables
    """
    result = {}
    for step in substitutions:
        # Update context for this step's evaluation
        current_context = {**context, **result}

        if step.functions:
            for alias, func_def in step.functions.items():
                match func_def:
                    case UserFunctionName():
                        result[alias] = wrap_function(func_def.root)
                    case UserFunctionKwargs():
                        result[alias] = wrap_function(func_def.name.root, default_kwargs=func_def.kwargs)
                    case _:
                        raise StageExecutionError(f"Invalid function definition for '{alias}': expected UserFunctionName or UserFunctionKwargs")
                logger.info(f"Seeded {alias} = {result[alias]}")

        if step.vars:
            for key, value in step.vars.items():
                resolved_value = pytest_httpchain_templates.substitution.walk(value, current_context)
                result[key] = resolved_value
                logger.info(f"Seeded {key} = {resolved_value}")

    return result


def call_user_function(func_call: Any, **extra_kwargs) -> Any:
    """Helper to call a user function from a UserFunctionCall model."""

    match func_call:
        case UserFunctionName():
            # Pydantic model with just function name
            return call_function(func_call.root, **extra_kwargs)
        case UserFunctionKwargs():
            # Pydantic model with name and kwargs
            merged_kwargs = {**func_call.kwargs, **extra_kwargs}
            return call_function(func_call.name.root, **merged_kwargs)
        case _:
            raise StageExecutionError(f"Invalid function call format: {func_call}")
