"""Exceptions for pytest-httpchain."""


class HTTPChainError(Exception):
    """Base exception for all pytest-httpchain errors."""


class RequestError(HTTPChainError):
    """An error making HTTP request."""


class ResponseError(HTTPChainError):
    """An error processing HTTP response."""


class VerificationError(HTTPChainError):
    """An error during response verification."""
