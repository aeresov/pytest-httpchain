from typing import Any

from requests.auth import AuthBase

from pytest_httpchain_userfunc.base import UserFunctionHandler
from pytest_httpchain_userfunc.exceptions import UserFunctionError


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
    result = UserFunctionHandler.call_function(name, **kwargs)
    if not isinstance(result, AuthBase):
        raise UserFunctionError(f"Auth function '{name}' must return AuthBase instance, got {type(result).__name__}") from None
    return result
