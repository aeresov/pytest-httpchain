import importlib
import inspect
import re
from collections.abc import Callable
from typing import Any

from pytest_httpchain_userfunc.exceptions import UserFunctionError


class UserFunctionHandler:
    """Base class for handling user-defined functions."""

    NAME_PATTERN = re.compile(r"^(?:(?P<module>[a-zA-Z_][a-zA-Z0-9_.]*):)?(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$")

    @classmethod
    def parse_function_name(cls, func_name: str) -> tuple[str | None, str]:
        match = cls.NAME_PATTERN.match(func_name)
        if not match:
            raise UserFunctionError(f"Invalid function name format: {func_name}") from None

        return match.group("module"), match.group("function")

    @classmethod
    def import_function(cls, module_path: str | None, function_name: str) -> Callable[..., Any]:
        """Import a function from a module or search in conftest/globals.

        Args:
            module_path: Module to import from (None to search conftest/globals)
            function_name: Name of the function to import

        Returns:
            The imported function

        Raises:
            UserFunctionError: If the function cannot be found
        """
        if module_path is None:
            # Try to import from conftest module first
            try:
                conftest = importlib.import_module("conftest")
                if hasattr(conftest, function_name):
                    func = getattr(conftest, function_name)
                    if callable(func):
                        return func
            except ImportError:
                pass

            # Try to find in the current test module's globals
            frame = inspect.currentframe()
            if frame is not None:
                # Walk up the call stack to find the frame that has our function
                while frame is not None:
                    if function_name in frame.f_globals:
                        func = frame.f_globals[function_name]
                        if callable(func):
                            return func
                    frame = frame.f_back

            raise UserFunctionError(f"Function '{function_name}' not found in conftest or current scope") from None
        else:
            # Import from specified module
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
    def _call_function(cls, name: str, *args, **kwargs) -> Any:
        """Parse, import and call a function.

        Args:
            name: Function name in format "module.path:function_name" or "function_name"
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Result of the function call

        Raises:
            UserFunctionError: If function cannot be called
        """
        module_name, function_name = cls.parse_function_name(name)
        func = cls.import_function(module_name, function_name)

        try:
            return func(*args, **kwargs)
        except Exception as e:
            raise UserFunctionError(f"Error calling function '{name}'") from e
