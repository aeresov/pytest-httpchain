"""JSON loading with reference resolution and deep merging.

``$include``/``$merge`` (preferred, since VS Code treats ``$ref`` specially) and
the legacy ``$ref`` behave identically, and point at another local file, a JSON
pointer within the document, or both — under a parent-traversal sandbox. Every
file is JSONC: comments and trailing commas are allowed (`loads_jsonc`).

    >>> # {"$include": "base.json", "url": "https://example.com"}
    >>> data = load_json(Path("test_scenario.http.json"))
"""

from pytest_httpchain.jsonref.equality import json_equal
from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, FileLoadError, InvalidJSONError, ReferenceResolverError
from pytest_httpchain.jsonref.jsonc import loads_jsonc, strip_jsonc
from pytest_httpchain.jsonref.loader import load_json
from pytest_httpchain.jsonref.plumbing.reference import REF_KEYS

__all__ = [
    "load_json",
    "REF_KEYS",
    "ReferenceResolverError",
    "InvalidJSONError",
    "FileLoadError",
    "DuplicateKeyError",
    "json_equal",
    "loads_jsonc",
    "strip_jsonc",
]
