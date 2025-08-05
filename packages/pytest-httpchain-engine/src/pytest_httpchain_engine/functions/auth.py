"""Authentication function handling."""

from typing import Any

from pytest_httpchain_engine.functions.base import UserFunctionHandler


class AuthFunction(UserFunctionHandler):
    """Handles authentication functions for HTTP requests."""

    @classmethod
    def call(cls, name: str) -> Any:
        """Call an authentication function by name.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"

        Returns:
            Authentication object (e.g., requests auth instance)
        """
        return cls.call_function(name)

    @classmethod
    def call_with_kwargs(cls, name: str, kwargs: dict[str, Any]) -> Any:
        """Call an authentication function with keyword arguments.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"
            kwargs: Keyword arguments to pass to the function

        Returns:
            Authentication object (e.g., requests auth instance)
        """
        return cls.call_function(name, **kwargs)
