"""JSON file loading with reference resolution."""

from pathlib import Path
from typing import Any

from pytest_httpchain_jsonref.plumbing.reference import ReferenceResolver


def load_json(path: Path, max_parent_traversal_depth: int = 3) -> dict[str, Any]:
    """Load JSON from file and resolve all $ref statements with circular reference protection.

    Args:
        path: Path to the JSON file to load
        max_parent_traversal_depth: Maximum number of parent directory traversals allowed in $ref paths

    Returns:
        Dictionary with all $ref statements resolved

    Raises:
        ReferenceResolverError: If the file cannot be loaded or parsed, if merge conflicts occur, or if circular references are detected
    """
    resolver = ReferenceResolver(max_parent_traversal_depth)
    return resolver.resolve_file(path)
