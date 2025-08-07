import importlib
import inspect
import re
from collections.abc import Callable
from typing import Any, TypeVar

from pytest_httpchain_userfunc.exceptions import UserFunctionError

T = TypeVar("T")


class UserFunctionHandler:
    """Handles user-defined function importing and execution."""

    NAME_PATTERN = re.compile(r"^(?:(?P<module>[a-zA-Z_][a-zA-Z0-9_.]*):)?(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$")

    @classmethod
    def call_function(cls, name: str, *args, **kwargs) -> Any:
        """Import and call a user function.

        Args:
            name: Function name in format "module.path:function_name" or "function_name"
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Result of the function call

        Raises:
            UserFunctionError: If function cannot be imported or called
        """
        module_name, function_name = cls._parse_name(name)
        func = cls._import(module_name, function_name)

        try:
            return func(*args, **kwargs)
        except Exception as e:
            raise UserFunctionError(f"Error calling function '{name}'") from e

    @classmethod
    def _parse_name(cls, func_name: str) -> tuple[str | None, str]:
        """Parse function name into module and function parts."""
        match = cls.NAME_PATTERN.match(func_name)
        if not match:
            raise UserFunctionError(f"Invalid function name format: {func_name}") from None
        return match.group("module"), match.group("function")

    @classmethod
    def _import(cls, module_path: str | None, function_name: str) -> Callable[..., Any]:
        """Import a function from module or search in conftest/globals."""
        if module_path:
            return cls._import_from_module(module_path, function_name)

        func = cls._try_conftest(function_name)
        if func:
            return func

        func = cls._try_current_scope(function_name)
        if func:
            return func

        raise UserFunctionError(f"Function '{function_name}' not found in conftest or current scope") from None

    @classmethod
    def _import_from_module(cls, module_path: str, function_name: str) -> Callable[..., Any]:
        """Import function from specific module."""
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise UserFunctionError(f"Failed to import module '{module_path}'") from e

        if not hasattr(module, function_name):
            raise UserFunctionError(f"Function '{function_name}' not found in module '{module_path}'") from None

        func = getattr(module, function_name)
        if not callable(func):
            raise UserFunctionError(f"'{module_path}:{function_name}' is not a callable function") from None

        return func

    @classmethod
    def _try_conftest(cls, function_name: str) -> Callable[..., Any] | None:
        """Try to import function from conftest module."""
        try:
            conftest = importlib.import_module("conftest")
            func = getattr(conftest, function_name, None)
            return func if callable(func) else None
        except ImportError:
            return None

    @classmethod
    def _try_current_scope(cls, function_name: str) -> Callable[..., Any] | None:
        """Try to find function in current scope by walking up frames."""
        frame = inspect.currentframe()
        while frame:
            if function_name in frame.f_globals:
                func = frame.f_globals[function_name]
                if callable(func):
                    return func
            frame = frame.f_back
        return None

    @classmethod
    def get_function(cls, name: str, protocol: type[T] | None = None) -> T | Callable[..., Any]:
        """Import a function and optionally validate against a protocol.

        Args:
            name: Function name in format "module.path:function_name" or "function_name"
            protocol: Optional Protocol class to validate against

        Returns:
            The imported function, optionally type-checked against protocol

        Raises:
            UserFunctionError: If function cannot be imported or doesn't match protocol
        """
        module_name, function_name = cls._parse_name(name)
        func = cls._import(module_name, function_name)

        if protocol and not isinstance(func, protocol):
            raise UserFunctionError(f"Function '{name}' does not match expected protocol {protocol.__name__}") from None

        return func
