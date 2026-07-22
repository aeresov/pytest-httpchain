"""User function handling for pytest-httpchain.

This module provides utilities for importing and invoking user-defined functions
from test scenarios. Functions must be specified as explicit module paths in
"module.submodule:func" format. Bare function names (without a module path) are
rejected with UserFunctionError.

Example:
    >>> from pytest_httpchain.userfunc import call_function
    >>> result = call_function("mymodule:my_auth_handler")

Key Behaviors
-------------
``wrap_function``'s ``default_kwargs`` are merged with call-time kwargs, with
call-time kwargs winning on conflicts.

Import failures and runtime call failures append the underlying exception to
the message (``...: {cause}``) and also chain it as ``__cause__``. Consumers in
the main plugin render only the message text (stage failures use
``pytrace=False``; the validator embeds ``str(e)``), so the cause must live in
the message -- not just the chain -- to be visible.
"""

import importlib
import re
from collections.abc import Callable
from typing import Any

from pytest_httpchain.constants import USER_FUNCTION_NAME_PATTERN
from pytest_httpchain.errors import HttpChainError, StageExecutionError
from pytest_httpchain.models import UserFunctionCall, UserFunctionKwargs, UserFunctionName


class UserFunctionError(HttpChainError):
    """Error importing or calling a user-supplied function."""


# The grammar lives in constants (bottom layer) so both the models' validator
# and this importer share one encoding without pinning this module below models.
NAME_PATTERN = USER_FUNCTION_NAME_PATTERN


def import_function(name: str) -> Callable[..., Any]:
    """Import a function by name.

    Args:
        name: Function name in "module.path:function_name" format

    Returns:
        The imported callable function

    Raises:
        UserFunctionError: If function cannot be found or imported
    """
    match = NAME_PATTERN.match(name)
    if not match:
        # Keep the actionable hint for the most common mistake — a bare
        # function name without its module path.
        if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name):
            raise UserFunctionError(f"Module path is required: use 'module:{name}' format instead of '{name}'")
        raise UserFunctionError(f"Invalid function name format: {name}")

    module_path = match.group("module")
    function_name = match.group("function")

    try:
        module = importlib.import_module(module_path)
    except Exception as e:
        # Importing a user module runs its top-level code, which can raise
        # anything (not just ImportError); wrap all of it so the failure goes
        # through the curated UserFunctionError path instead of a raw traceback.
        raise UserFunctionError(f"Failed to import module '{module_path}': {e}") from e

    if not hasattr(module, function_name):
        raise UserFunctionError(f"Function '{function_name}' not found in module '{module_path}'")

    func = getattr(module, function_name)
    if not callable(func):
        raise UserFunctionError(f"'{module_path}:{function_name}' is not callable")

    return func


def call_function(name: str, /, *args, **kwargs) -> Any:
    """Import and call a user function.

    Args:
        name: Function name in "module.path:function_name" format (positional-only)
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
    except UserFunctionError:
        # Already curated (e.g. the user function called another httpchain helper);
        # propagate as-is instead of double-wrapping. Mirrors wrap_function.
        raise
    except Exception as e:
        raise UserFunctionError(f"Error calling function '{name}': {e}") from e


def wrap_function(name: str, /, default_kwargs: dict[str, Any] | None = None) -> Callable[..., Any]:
    """Create a wrapped callable for a user function.

    The wrapped function can be called directly in template expressions.
    Default kwargs are merged with call-time kwargs (call-time wins).

    Args:
        name: Function name in "module.path:function_name" format (positional-only)
        default_kwargs: Optional default keyword arguments

    Returns:
        A callable that loads and executes the user function
    """
    # Normalize to non-None values for type checker
    default_kwargs_dict: dict[str, Any] = default_kwargs if default_kwargs is not None else {}

    def wrapped(*args, **kwargs):
        try:
            func = import_function(name)
            # Merge default kwargs with call-time kwargs (call-time wins)
            merged_kwargs = {**default_kwargs_dict, **kwargs}
            return func(*args, **merged_kwargs)
        except UserFunctionError:
            raise
        except Exception as e:
            raise UserFunctionError(f"Error calling function '{name}': {e}") from e

    # Set a meaningful name for debugging
    wrapped.__name__ = f"wrapped_{name.replace(':', '_').replace('.', '_')}"
    return wrapped


def call_user_function(func_call: UserFunctionCall, **extra_kwargs: Any) -> object:
    """Import and call a user function described by a ``UserFunctionCall`` model.

    A bare ``UserFunctionName`` is called with only ``extra_kwargs``; a
    ``UserFunctionKwargs`` merges its declared kwargs under ``extra_kwargs``
    (caller-supplied values win on conflict). Used both for request/scenario auth
    callables and for verify/save user functions, where ``extra_kwargs`` carries
    the ``response``. Raises ``StageExecutionError`` if ``func_call`` is neither
    supported shape. Lives here (not utils) because it dispatches on the model
    union — this module sits above models exactly so it can own that dispatch.
    """
    match func_call:
        case UserFunctionName():
            return call_function(func_call.root, **extra_kwargs)
        case UserFunctionKwargs():
            merged_kwargs = {**func_call.kwargs, **extra_kwargs}
            return call_function(func_call.name.root, **merged_kwargs)
        case _:
            raise StageExecutionError(f"Invalid function call format: {func_call}")


__all__ = [
    "NAME_PATTERN",
    "call_user_function",
    "import_function",
    "call_function",
    "wrap_function",
    "UserFunctionError",
]
