import importlib
import re
from collections.abc import Callable
from typing import Any

import requests
from requests.auth import AuthBase


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
    def validate_name(cls, func_name: str) -> str:
        module_path, function_name = cls._parse_given_name(func_name)
        cls._import_function(module_path, function_name)
        # expect ValueError raised here on any error
        return func_name

    @classmethod
    def call_with_kwargs(cls, func_name: str, response: requests.Response, kwargs: dict[str, Any] | None = None) -> Any:
        module_path, function_name = cls._parse_given_name(func_name)
        func = cls._import_function(module_path, function_name)

        if kwargs:
            return func(response, **kwargs)
        else:
            return func(response)

    @classmethod
    def call_auth_function(cls, func_name: str, kwargs: dict[str, Any] | None = None) -> AuthBase:
        """
        Call an authentication function that returns a requests.AuthBase instance.

        Args:
            func_name: Function name in 'module:function' format
            kwargs: Optional keyword arguments to pass to the function

        Returns:
            AuthBase: Authentication instance to be used with requests

        Raises:
            ValueError: If function doesn't exist or doesn't return AuthBase instance
        """
        module_path, function_name = cls._parse_given_name(func_name)
        func = cls._import_function(module_path, function_name)

        try:
            if kwargs:
                auth_instance = func(**kwargs)
            else:
                auth_instance = func()
        except Exception as e:
            raise ValueError(f"Error calling authentication function '{func_name}': {e}") from e

        if not isinstance(auth_instance, AuthBase):
            raise ValueError(f"Authentication function '{func_name}' must return a requests.AuthBase instance, got {type(auth_instance)}")

        return auth_instance

    @classmethod
    def call_auth_function_from_spec(cls, auth_spec: str | Any) -> AuthBase:
        """
        Call an authentication function from either a string or FunctionCall spec.

        Args:
            auth_spec: Either a function name string or FunctionCall object

        Returns:
            AuthBase: Authentication instance to be used with requests

        Raises:
            ValueError: If function doesn't exist or doesn't return AuthBase instance
        """
        if isinstance(auth_spec, str):
            # Simple function name, call without kwargs
            return cls.call_auth_function(auth_spec)
        else:
            # FunctionCall object with kwargs (already resolved by variable substitution)
            func_name = auth_spec.function
            kwargs = auth_spec.kwargs

            return cls.call_auth_function(func_name, kwargs)
