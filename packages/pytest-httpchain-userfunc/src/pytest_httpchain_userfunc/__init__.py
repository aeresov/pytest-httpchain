"""User function handling for pytest-httpchain.

This package provides utilities for importing and invoking user-defined functions
from test scenarios. Functions must be specified as explicit module paths in
"module.submodule:func" format. Bare function names (without a module path) are
rejected with UserFunctionError.

Example:
    >>> from pytest_httpchain_userfunc import call_function
    >>> result = call_function("mymodule:my_auth_handler")
"""

from .exceptions import UserFunctionError
from .userfunc import (
    NAME_PATTERN,
    call_function,
    import_function,
    wrap_function,
)

__all__ = [
    "NAME_PATTERN",
    "import_function",
    "call_function",
    "wrap_function",
    "UserFunctionError",
]
