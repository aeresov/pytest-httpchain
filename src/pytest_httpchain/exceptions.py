"""Exceptions for pytest-httpchain."""

from pytest_httpchain_engine.exceptions import HTTPChainError


class RequestError(HTTPChainError):
    """An error making HTTP request."""


class ResponseError(HTTPChainError):
    """An error processing HTTP response."""


class VerificationError(HTTPChainError):
    """An error during response verification."""
