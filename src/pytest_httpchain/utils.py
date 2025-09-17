"""Utilities for handling UserFunctionCall models.

This module provides helpers for calling user functions from UserFunctionCall
models which can be in various formats (string, dict, or Pydantic models).
"""

from typing import Any

from pytest_httpchain_models.entities import UserFunctionKwargs, UserFunctionName
from pytest_httpchain_userfunc import call_function

from .exceptions import StageExecutionError


def call_user_function(func_call: Any, **extra_kwargs) -> Any:
    """Helper to call a user function from a UserFunctionCall model.

    Args:
        func_call: A UserFunctionCall which can be:
            - str: Plain function name string
            - dict: Function definition with 'function' and optional 'kwargs'
            - UserFunctionName: Pydantic model with function name
            - UserFunctionKwargs: Pydantic model with function and kwargs
        **extra_kwargs: Additional kwargs to pass to the function

    Returns:
        The result of calling the function

    Raises:
        StageExecutionError: If the function call format is invalid
    """

    match func_call:
        case str():
            # Plain function name string
            return call_function(func_call, **extra_kwargs)
        case UserFunctionKwargs():
            # Pydantic model with function and kwargs
            merged_kwargs = {**func_call.kwargs, **extra_kwargs}
            return call_function(func_call.function.root, **merged_kwargs)
        case UserFunctionName():
            # Pydantic model with just function name
            return call_function(func_call.root, **extra_kwargs)
        case dict():
            # Dict with 'function' and optional 'kwargs'
            func_name = func_call.get("function")
            if not func_name:
                raise StageExecutionError("Function definition must have 'function' key")
            kwargs = func_call.get("kwargs", {})
            merged_kwargs = {**kwargs, **extra_kwargs}
            return call_function(func_name, **merged_kwargs)
        case _:
            raise StageExecutionError(f"Invalid function call format: {func_call}")
