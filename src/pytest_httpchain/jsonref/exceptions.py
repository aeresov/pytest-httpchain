from pytest_httpchain.errors import HttpChainError


class ReferenceResolverError(HttpChainError):
    """Exception for reference resolution errors."""


class DuplicateKeyError(ReferenceResolverError):
    """A JSON object contains the same key twice.

    A distinct subclass so consumers (the validator) can report it as a JSON
    *content* problem (``HTTPCHAIN014``) instead of a $ref-resolution one — no
    reference is involved in a duplicated key.
    """
