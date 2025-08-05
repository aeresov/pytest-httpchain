"""Exception classes for pytest-httpchain."""


class HTTPChainError(Exception):
    """Base exception for all pytest-httpchain errors."""


class LoaderError(HTTPChainError):
    """An error parsing JSON test scenario."""


class SubstitutionError(HTTPChainError):
    """An error during template substitution."""


class ValidationError(HTTPChainError):
    """An error validating data against schema or rules."""


class UserFunctionError(HTTPChainError):
    """An error calling user-defined function."""
