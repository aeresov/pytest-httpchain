"""Generic function wrapper for substitution context.

This module provides utilities to wrap user-defined functions for use
in template substitutions and simpleeval expressions.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .base import UserFunctionHandler
from .exceptions import UserFunctionError

if TYPE_CHECKING:
    from pytest_httpchain_models.entities import UserFunctionCall


def create_wrapped_function(func_definition: "UserFunctionCall") -> Callable[..., Any]:
    """Create a wrapped callable for a user function definition.

    This wrapper handles both simple function names and functions with kwargs.
    The wrapped function can be called directly in template expressions.

    Args:
        func_definition: Either UserFunctionName or UserFunctionKwargs

    Returns:
        A callable that loads and executes the user function

    Example:
        >>> # For a simple function
        >>> func_def = UserFunctionName(root="math:sqrt")
        >>> wrapped = create_wrapped_function(func_def)
        >>> result = wrapped(16)  # Returns 4.0

        >>> # For a function with default kwargs
        >>> func_def = UserFunctionKwargs(
        ...     function=UserFunctionName(root="formatter:format_date"),
        ...     kwargs={"format": "%Y-%m-%d"}
        ... )
        >>> wrapped = create_wrapped_function(func_def)
        >>> result = wrapped("2024-01-15T10:30:00")  # Uses default format
        >>> result = wrapped("2024-01-15T10:30:00", format="%d/%m/%Y")  # Override
    """

    def wrapped_function(*args, **kwargs):
        """Execute the wrapped user function."""
        try:
            # Check if it has a 'kwargs' attribute (UserFunctionKwargs)
            if hasattr(func_definition, "kwargs"):
                # Merge default kwargs with call-time kwargs (call-time wins)
                merged_kwargs = {**func_definition.kwargs, **kwargs}
                return UserFunctionHandler.call_function(func_definition.function.root, *args, **merged_kwargs)
            else:
                # Simple function name (UserFunctionName)
                return UserFunctionHandler.call_function(func_definition.root, *args, **kwargs)
        except UserFunctionError:
            # Re-raise UserFunctionError as-is
            raise
        except Exception as e:
            # Wrap other exceptions
            func_name = func_definition.function.root if hasattr(func_definition, "function") else func_definition.root
            raise UserFunctionError(f"Error calling function '{func_name}': {str(e)}") from e

    # Set a meaningful name for debugging
    if hasattr(func_definition, "function"):
        wrapped_function.__name__ = f"wrapped_{func_definition.function.root.replace(':', '_')}"
    else:
        wrapped_function.__name__ = f"wrapped_{func_definition.root.replace(':', '_')}"

    return wrapped_function


def wrap_functions_dict(functions: dict[str, "UserFunctionCall"]) -> dict[str, Callable[..., Any]]:
    """Wrap a dictionary of function definitions.

    This is a convenience function for wrapping multiple functions at once,
    typically used for scenario.substitutions.functions.

    Args:
        functions: Dictionary mapping names to function definitions

    Returns:
        Dictionary mapping names to wrapped callables

    Example:
        >>> functions = {
        ...     "double": UserFunctionName(root="math_utils:double"),
        ...     "format": UserFunctionKwargs(
        ...         function=UserFunctionName(root="formatters:format_number"),
        ...         kwargs={"decimals": 2}
        ...     )
        ... }
        >>> wrapped = wrap_functions_dict(functions)
        >>> wrapped["double"](5)  # Returns 10
        >>> wrapped["format"](3.14159)  # Returns "3.14"
    """
    return {name: create_wrapped_function(func_def) for name, func_def in functions.items()}
