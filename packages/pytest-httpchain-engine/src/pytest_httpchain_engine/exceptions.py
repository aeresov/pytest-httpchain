"""Exception classes for pytest-httpchain."""


class HTTPChainError(Exception):
    """Base exception for all pytest-httpchain errors."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class LoaderError(HTTPChainError):
    """An error parsing JSON test scenario."""


class SubstitutionError(HTTPChainError):
    """An error during template substitution."""


class TesterError(HTTPChainError):
    """An error making HTTP call or processing response."""


class ValidationError(HTTPChainError):
    """An error validating data against schema or rules."""


class UserFunctionError(HTTPChainError):
    """An error calling user-defined function."""
