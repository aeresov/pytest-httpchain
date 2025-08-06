from typing import Any

from requests.auth import AuthBase

from pytest_httpchain_userfunc.base import UserFunctionHandler


class AuthFunction(UserFunctionHandler):
    """Handles authentication functions for HTTP requests."""

    @classmethod
    def call(cls, name: str) -> AuthBase:
        """Call an authentication function by name.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"

        Returns:
            Authentication object (e.g., requests auth instance)
        """
        return cls._call_function(name)

    @classmethod
    def call_with_kwargs(cls, name: str, kwargs: dict[str, Any]) -> AuthBase:
        """Call an authentication function with keyword arguments.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"
            kwargs: Keyword arguments to pass to the function

        Returns:
            Authentication object (e.g., requests auth instance)
        """
        return cls._call_function(name, **kwargs)
