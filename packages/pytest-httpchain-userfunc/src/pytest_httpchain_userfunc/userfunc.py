"""Simplified user function handling for pytest-httpchain.

This module handles importing and wrapping user-defined functions from:
- Explicit module paths (module.submodule:func)
- conftest.py files
- Current execution scope
"""

import importlib
import inspect
import re
from collections.abc import Callable
from typing import Any

from .exceptions import UserFunctionError

NAME_PATTERN = re.compile(r"^(?:(?P<module>[a-zA-Z_][a-zA-Z0-9_.]*):)?(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$")


def import_function(name: str) -> Callable[..., Any]:
    """Import a function by name.

    Args:
        name: Function name in "module.path:function_name" or "function_name" format

    Returns:
        The imported callable function

    Raises:
        UserFunctionError: If function cannot be found or imported
    """
    match = NAME_PATTERN.match(name)
    if not match:
        raise UserFunctionError(f"Invalid function name format: {name}")

    module_path = match.group("module")
    function_name = match.group("function")

    # If module specified, import from there
    if module_path:
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise UserFunctionError(f"Failed to import module '{module_path}'") from e

        if not hasattr(module, function_name):
            raise UserFunctionError(f"Function '{function_name}' not found in module '{module_path}'")

        func = getattr(module, function_name)
        if not callable(func):
            raise UserFunctionError(f"'{module_path}:{function_name}' is not callable")

        return func

    # Try conftest
    try:
        conftest = importlib.import_module("conftest")
        if hasattr(conftest, function_name):
            func = getattr(conftest, function_name)
            if callable(func):
                return func
    except ImportError:
        pass

    # Try current scope by walking up frames
    frame = inspect.currentframe()
    while frame:
        if function_name in frame.f_globals:
            func = frame.f_globals[function_name]
            if callable(func):
                return func
        frame = frame.f_back

    raise UserFunctionError(f"Function '{function_name}' not found in conftest or current scope")


def call_function(name: str, /, *args, **kwargs) -> Any:
    """Import and call a user function.

    Args:
        name: Function name in "module.path:function_name" or "function_name" format (positional-only)
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function

    Returns:
        Result of the function call

    Raises:
        UserFunctionError: If function cannot be imported or called
    """
    func = import_function(name)

    try:
        return func(*args, **kwargs)
    except Exception as e:
        raise UserFunctionError(f"Error calling function '{name}'") from e


def wrap_function(name: str, /, default_args: list[Any] | None = None, default_kwargs: dict[str, Any] | None = None) -> Callable[..., Any]:
    """Create a wrapped callable for a user function.

    The wrapped function can be called directly in template expressions.
    Default args are prepended to call-time args.
    Default kwargs are merged with call-time kwargs (call-time wins).

    Args:
        func_name: Function name in "module.path:function_name" or "function_name" format (positional-only)
        default_args: Optional default positional arguments
        default_kwargs: Optional default keyword arguments

    Returns:
        A callable that loads and executes the user function
    """
    # Normalize to non-None values for type checker
    default_args_list: list[Any] = default_args if default_args is not None else []
    default_kwargs_dict: dict[str, Any] = default_kwargs if default_kwargs is not None else {}

    def wrapped(*args, **kwargs):
        try:
            func = import_function(name)
            # Prepend default args to call-time args
            merged_args = default_args_list + list(args)
            # Merge default kwargs with call-time kwargs (call-time wins)
            merged_kwargs = {**default_kwargs_dict, **kwargs}
            return func(*merged_args, **merged_kwargs)
        except UserFunctionError:
            raise
        except Exception as e:
            raise UserFunctionError(f"Error calling function '{name}': {str(e)}") from e

    # Set a meaningful name for debugging
    wrapped.__name__ = f"wrapped_{name.replace(':', '_').replace('.', '_')}"
    return wrapped
