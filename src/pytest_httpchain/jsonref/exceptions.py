from pytest_httpchain.errors import HttpChainError


class ReferenceResolverError(HttpChainError):
    """Exception for reference resolution errors."""


class DuplicateKeyError(ReferenceResolverError):
    """A JSON object contains the same key twice. Its own subclass so the
    validator can report it as a JSON content problem, not a $ref one."""
