"""User function handling for pytest-httpchain.

This package provides utilities for importing and invoking user-defined functions
from test scenarios. Functions can be specified as:
- Explicit module paths: "module.submodule:func"
- conftest.py functions: "func_name"
- Current scope functions: "func_name"

Example:
    >>> from pytest_httpchain_userfunc import call_function
    >>> result = call_function("mymodule:my_auth_handler")
"""

from .exceptions import UserFunctionError
from .userfunc import (
    call_function,
    import_function,
    wrap_function,
)

__all__ = [
    "import_function",
    "call_function",
    "wrap_function",
    "UserFunctionError",
]
