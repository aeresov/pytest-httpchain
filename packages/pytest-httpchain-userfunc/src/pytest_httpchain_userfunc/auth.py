from typing import Any

from requests.auth import AuthBase

from pytest_httpchain_userfunc.base import UserFunctionHandler
from pytest_httpchain_userfunc.exceptions import UserFunctionError
from pytest_httpchain_userfunc.protocols import AuthFunction


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
