"""Importing and invoking user functions named ``"module.submodule:func"``.

Failures raise `UserFunctionError` with the cause appended to the message, not
just chained: consumers render only the message text (stage failures use
``pytrace=False``, the validator embeds ``str(e)``).
"""

import importlib
from collections.abc import Callable
from typing import Any

from pytest_httpchain.constants import USER_FUNCTION_NAME_PATTERN, user_function_name_problem
from pytest_httpchain.errors import HttpChainError
from pytest_httpchain.models import UserFunctionCall, UserFunctionKwargs, UserFunctionName


class UserFunctionError(HttpChainError):
    """Error importing or calling a user-supplied function."""


# Shared with the models' validator; lives in constants so neither module has to
# sit below the other.
NAME_PATTERN = USER_FUNCTION_NAME_PATTERN


def import_function(name: str) -> Callable[..., Any]:
    """Import a ``"module.path:function_name"`` function."""
    if (problem := user_function_name_problem(name)) is not None:
        raise UserFunctionError(problem)

    match = NAME_PATTERN.match(name)
    assert match is not None, "user_function_name_problem() returns None only for names NAME_PATTERN matches"

    module_path = match.group("module")
    function_name = match.group("function")

    try:
        module = importlib.import_module(module_path)
    except Exception as e:
        # Importing runs the module's top-level code, which can raise anything.
        raise UserFunctionError(f"Failed to import module '{module_path}': {e}") from e

    if not hasattr(module, function_name):
        raise UserFunctionError(f"Function '{function_name}' not found in module '{module_path}'")

    func = getattr(module, function_name)
    if not callable(func):
        raise UserFunctionError(f"'{module_path}:{function_name}' is not callable")

    return func


def call_function(name: str, /, *args, **kwargs) -> Any:
    """Import and call a user function."""
    func = import_function(name)

    try:
        return func(*args, **kwargs)
    except UserFunctionError:
        # Already curated: propagate rather than double-wrap.
        raise
    except Exception as e:
        raise UserFunctionError(f"Error calling function '{name}': {e}") from e


def wrap_function(name: str, /, default_kwargs: dict[str, Any] | None = None) -> Callable[..., Any]:
    """A callable that imports and runs a user function, for use inside template
    expressions. Call-time kwargs win over ``default_kwargs``."""
    default_kwargs_dict: dict[str, Any] = default_kwargs if default_kwargs is not None else {}

    def wrapped(*args, **kwargs):
        return call_function(name, *args, **{**default_kwargs_dict, **kwargs})

    wrapped.__name__ = f"wrapped_{name.replace(':', '_').replace('.', '_')}"
    return wrapped


def call_target(func_call: UserFunctionCall) -> tuple[str, dict[str, Any]]:
    """Destructure a ``UserFunctionCall`` into ``(import name, declared kwargs)``.

    The single dispatch over the call union, shared by `call_user_function` and
    the validator's deep checks.
    """
    match func_call:
        case UserFunctionName():
            return str(func_call.root), {}
        case UserFunctionKwargs():
            return str(func_call.name.root), dict(func_call.kwargs)
        case _:
            raise RuntimeError(f"Unhandled function call: {type(func_call).__name__}")


def call_user_function(func_call: UserFunctionCall, **extra_kwargs: Any) -> object:
    """Import and call a user function described by a model.

    Declared kwargs merge under ``extra_kwargs``, which carries the ``response``
    for verify/save functions.
    """
    name, kwargs = call_target(func_call)
    return call_function(name, **{**kwargs, **extra_kwargs})


__all__ = [
    "NAME_PATTERN",
    "call_target",
    "call_user_function",
    "import_function",
    "call_function",
    "wrap_function",
    "UserFunctionError",
]
