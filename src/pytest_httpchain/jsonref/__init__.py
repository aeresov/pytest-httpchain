"""JSON reference resolution for pytest-httpchain.

This package provides JSON loading with reference resolution and deep merging
support. References use the ``$include``/``$merge`` directives (preferred, they
avoid conflicts with VS Code's JSON Schema validation) or the legacy ``$ref``;
all three behave identically. References can point to other local files or to
JSON pointers within the same document, with security controls for parent
directory traversal.

Example:
    >>> from pathlib import Path
    >>> from pytest_httpchain.jsonref import load_json
    >>> # base.json contains {"timeout": 30}; the scenario merges it in:
    >>> #   {"$include": "base.json", "url": "https://example.com"}
    >>> data = load_json(Path("test_scenario.http.json"))
"""

from .exceptions import DuplicateKeyError, ReferenceResolverError
from .loader import load_json

__all__ = [
    "load_json",
    "ReferenceResolverError",
    "DuplicateKeyError",
]
