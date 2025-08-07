from typing import Any

import requests

from pytest_httpchain_userfunc.base import UserFunctionHandler
from pytest_httpchain_userfunc.exceptions import UserFunctionError
from pytest_httpchain_userfunc.protocols import SaveFunction


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
