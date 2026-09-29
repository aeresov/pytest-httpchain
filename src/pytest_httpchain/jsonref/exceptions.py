from pathlib import Path

from pytest_httpchain.errors import HttpChainError


class ReferenceResolverError(HttpChainError):
    """Exception for reference resolution errors."""


class FileLoadError(ReferenceResolverError):
    """A file could not be read or parsed: the ``OSError``, ``JSONDecodeError``
    or ``RecursionError`` is the ``__cause__``. ``path`` is that file, the
    document itself or one a reference named (the innermost, when references
    nest), so a consumer can say which file a syntax error's line and column
    are in: the message names it, the cause alone does not."""

    def __init__(self, message: str, path: Path | None = None):
        super().__init__(message)
        self.path = path


class InvalidJSONError(ReferenceResolverError):
    """A file's content is not JSON the loader can read: bytes that are not
    UTF-8, a duplicate object key, a number too long to convert. Its own
    subclass so the validator can report it as a JSON content problem, not a
    $ref one, naming the file it is in rather than the file that referenced it."""


class DuplicateKeyError(InvalidJSONError):
    """A JSON object contains the same key twice."""
