from typing import Any

import requests

from pytest_httpchain_userfunc.base import UserFunctionHandler
from pytest_httpchain_userfunc.exceptions import UserFunctionError
from pytest_httpchain_userfunc.protocols import VerifyFunction


def call_verify_function(name: str, response: requests.Response, **kwargs: Any) -> bool:
    """Call a verification function for HTTP response.

    Args:
        name: Function name in format "module.path:function_name" or "function_name"
        response: HTTP response object to verify
        **kwargs: Optional keyword arguments for the function

    Returns:
        Whether verification was successful

    Raises:
        UserFunctionError: If function returns invalid type
    """
    # Get function with protocol validation (checks callability and signature structure)
    func = UserFunctionHandler.get_function(name, protocol=VerifyFunction)

    # Call the function
    result = func(response, **kwargs)

    # Runtime check for bool (runtime_checkable protocols only verify structure, not type annotations)
    if not isinstance(result, bool):
        raise UserFunctionError(f"Verify function '{name}' must return bool, got {type(result).__name__}") from None
    return result
