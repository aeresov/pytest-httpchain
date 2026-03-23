from pytest_httpchain_core import HttpChainError


class UserFunctionError(HttpChainError):
    """Exception for user function errors."""
