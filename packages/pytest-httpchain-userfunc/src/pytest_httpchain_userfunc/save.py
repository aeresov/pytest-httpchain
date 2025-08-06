from typing import Any

import requests

from pytest_httpchain_userfunc.base import UserFunctionHandler


class SaveFunction(UserFunctionHandler):
    """Handles save functions for HTTP responses."""

    @classmethod
    def call(cls, name: str, response: requests.Response) -> dict[str, Any]:
        """Call a verification function by name.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"
            response: HTTP response object to verify

        Returns:
            Data to insert into common data context
        """
        return cls._call_function(name, response)

    @classmethod
    def call_with_kwargs(cls, name: str, response: requests.Response, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Call a verification function with keyword arguments.

        Args:
            name: Function name in format "module.path:function_name" or just "function_name"
            response: HTTP response object to verify
            kwargs: Keyword arguments to pass to the function

        Returns:
            Data to insert into common data context
        """
        return cls._call_function(name, response, **kwargs)
