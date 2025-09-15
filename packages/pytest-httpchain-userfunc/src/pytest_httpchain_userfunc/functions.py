from typing import Any

import requests
from requests.auth import AuthBase

from pytest_httpchain_userfunc.base import UserFunctionHandler
from pytest_httpchain_userfunc.exceptions import UserFunctionError
from pytest_httpchain_userfunc.protocols import AuthFunction, SaveFunction, VerifyFunction


def call_auth_function(name: str, **kwargs: Any) -> AuthBase:
    """Call an authentication function.

    Args:
        name: Function name in format "module.path:function_name" or "function_name"
        **kwargs: Optional keyword arguments for the function

    Returns:
        Authentication object (e.g., requests auth instance)

    Raises:
        UserFunctionError: If function returns invalid type
    """
    # Get function with protocol validation (checks callability and signature structure)
    func = UserFunctionHandler.get_function(name, protocol=AuthFunction)

    # Call the function
    result = func(**kwargs)

    # Runtime check for AuthBase (runtime_checkable protocols only verify structure, not type annotations)
    if not isinstance(result, AuthBase):
        raise UserFunctionError(f"Auth function '{name}' must return AuthBase instance, got {type(result).__name__}") from None
    return result


def call_save_function(name: str, response: requests.Response, **kwargs: Any) -> dict[str, Any]:
    """Call a save function for HTTP response.

    Args:
        name: Function name in format "module.path:function_name" or "function_name"
        response: HTTP response object to process
        **kwargs: Optional keyword arguments for the function

    Returns:
        Data to insert into common data context

    Raises:
        UserFunctionError: If function returns invalid type
    """
    # Get function with protocol validation (checks callability and signature structure)
    func = UserFunctionHandler.get_function(name, protocol=SaveFunction)

    # Call the function
    result = func(response, **kwargs)

    # Runtime check for dict (runtime_checkable protocols only verify structure, not type annotations)
    if not isinstance(result, dict):
        raise UserFunctionError(f"Save function '{name}' must return dict, got {type(result).__name__}") from None
    return result


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
