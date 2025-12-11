"""JSON reference ($ref) resolution for pytest-httpchain.

This package provides JSON loading with $ref resolution and deep merging support.
References can point to local files or external paths, with security controls
for parent directory traversal.

Example:
    >>> from pytest_httpchain_jsonref import load_json
    >>> data = load_json("test_scenario.http.json")
"""

from .exceptions import ReferenceResolverError
from .loader import load_json

__all__ = [
    "load_json",
    "ReferenceResolverError",
]
