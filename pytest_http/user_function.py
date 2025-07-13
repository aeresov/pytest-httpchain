import importlib
import re
from collections.abc import Callable
from typing import Any

import requests


class UserFunction:
    @staticmethod
    def _parse_given_name(func_name: str) -> tuple[str, str]:
        pattern = r"^(?P<module>[a-zA-Z_][a-zA-Z0-9_.]*):(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$"
        match = re.match(pattern, func_name)
        if not match:
            raise ValueError(f"'{func_name}' does not match 'module:function' syntax with valid identifiers")
        return match.group("module"), match.group("function")

    @staticmethod
    def _import_function(module_path: str, function_name: str) -> Callable[..., Any]:
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ValueError(f"Cannot import module '{module_path}': {e}") from e

        if not hasattr(module, function_name):
            raise ValueError(f"Function '{function_name}' not found in module '{module_path}'")

        func = getattr(module, function_name)
        if not callable(func):
            raise ValueError(f"'{function_name}' in module '{module_path}' is not callable")

        return func

    @classmethod
    def validate_function_name(cls, func_name: str) -> str:
        module_path, function_name = cls._parse_given_name(func_name)
        cls._import_function(module_path, function_name)
        # expect ValueError raised here on any error
        return func_name

    @classmethod
    def call_function_with_kwargs(cls, func_name: str, response: requests.Response, kwargs: dict[str, Any] | None = None) -> Any:
        module_path, function_name = cls._parse_given_name(func_name)
        func = cls._import_function(module_path, function_name)

        if kwargs:
            return func(response, **kwargs)
        else:
            return func(response)
