from pytest_httpchain.errors import HttpChainError


class ReferenceResolverError(HttpChainError):
    """Exception for reference resolution errors."""


class InvalidJSONError(ReferenceResolverError):
    """A file's content is not JSON the loader can read: bytes that are not
    UTF-8, a duplicate object key, a number too long to convert. Its own
    subclass so the validator can report it as a JSON content problem, not a
    $ref one, naming the file it is in rather than the file that referenced it."""


class DuplicateKeyError(InvalidJSONError):
    """A JSON object contains the same key twice."""
