from pytest_httpchain_core import HttpChainError


class TemplatesError(HttpChainError):
    """Exception for templating errors."""
