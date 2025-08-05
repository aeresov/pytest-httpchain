"""Verification function handling."""

from typing import Any

import requests

from pytest_httpchain_engine.functions.base import UserFunctionHandler


class VerificationFunction(UserFunctionHandler):
    """Handles verification functions for HTTP responses."""

    @classmethod
    def call(cls, name: str, response: requests.Response) -> Any:
        """Call a verification function by name.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"
            response: HTTP response object to verify

        Returns:
            Result of the verification function
        """
        return cls.call_function(name, response)

    @classmethod
    def call_with_kwargs(cls, name: str, response: requests.Response, kwargs: dict[str, Any]) -> Any:
        """Call a verification function with keyword arguments.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"
            response: HTTP response object to verify
            kwargs: Keyword arguments to pass to the function

        Returns:
            Result of the verification function
        """
        return cls.call_function(name, response, **kwargs)
