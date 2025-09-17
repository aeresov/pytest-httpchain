from typing import Any

from pytest_httpchain_models.entities import UserFunctionKwargs, UserFunctionName
from pytest_httpchain_userfunc import call_function

from .exceptions import StageExecutionError


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
