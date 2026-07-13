from pytest_httpchain.errors import HttpChainError


class TemplatesError(HttpChainError):
    """Exception for templating errors."""
