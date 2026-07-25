"""JSON loading with reference resolution and deep merging.

``$include``/``$merge`` (preferred, since VS Code treats ``$ref`` specially) and
the legacy ``$ref`` behave identically, and point at another local file, a JSON
pointer within the document, or both — under a parent-traversal sandbox.

    >>> # {"$include": "base.json", "url": "https://example.com"}
    >>> data = load_json(Path("test_scenario.http.json"))
"""

from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, ReferenceResolverError
from pytest_httpchain.jsonref.loader import load_json

__all__ = [
    "load_json",
    "ReferenceResolverError",
    "DuplicateKeyError",
]
