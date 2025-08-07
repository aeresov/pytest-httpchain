"""Helper functions for common patterns in pytest-httpchain."""

from collections.abc import Callable
from typing import Any

from pytest_httpchain_models.entities import UserFunctionCall, UserFunctionKwargs


def call_user_function(
    model: UserFunctionCall,
    call_function: Callable,
    *args: Any,
    **extra_kwargs: Any,
) -> Any:
    """Generic helper to call user functions from UserFunctionCall model.

    This helper eliminates the repeated pattern of checking whether a UserFunctionCall
    is a UserFunctionName or UserFunctionKwargs and calling the appropriate function.

    Args:
        model: Either UserFunctionName or UserFunctionKwargs
        call_function: The specific function caller (call_save_function, call_verify_function, etc.)
        *args: Positional arguments to pass to call_function (e.g., response object)
        **extra_kwargs: Additional keyword arguments for call_function

    Returns:
        Result from the called function

    Example:
        # Instead of:
        if isinstance(func_item, UserFunctionKwargs):
            result = call_save_function(func_item.function.root, response, **func_item.kwargs)
        else:
            result = call_save_function(func_item.root, response)

        # Use:
        result = call_user_function(func_item, call_save_function, response)
    """
    if isinstance(model, UserFunctionKwargs):
        kwargs = {**extra_kwargs, **model.kwargs}
        return call_function(model.function.root, *args, **kwargs)
    else:  # UserFunctionName
        return call_function(model.root, *args, **extra_kwargs)
