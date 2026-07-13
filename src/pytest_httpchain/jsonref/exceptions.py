from pytest_httpchain.errors import HttpChainError


class ReferenceResolverError(HttpChainError):
    """Exception for reference resolution errors."""
