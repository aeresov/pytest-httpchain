"""JSON file loading with reference resolution."""

from pathlib import Path
from typing import Any

from pytest_httpchain_jsonref.plumbing.reference import ReferenceResolver


def load_json(path: Path, max_parent_traversal_depth: int = 3, root_path: Path | None = None) -> dict[str, Any]:
    """Load JSON from file and resolve all $include/$merge/$ref statements with circular reference protection.

    All three directives ($include, $merge, $ref) work identically. $include and $merge are preferred
    as they avoid conflicts with VS Code's JSON Schema validation (which treats $ref specially).

    Args:
        path: Path to the JSON file to load
        max_parent_traversal_depth: Maximum number of parent directory traversals allowed in reference paths
        root_path: Optional root directory for resolving references (e.g., pytest's rootdir)

    Returns:
        Dictionary with all $include/$ref statements resolved

    Raises:
        ReferenceResolverError: If the file cannot be loaded or parsed, if merge conflicts occur, or if circular references are detected
    """
    resolver = ReferenceResolver(max_parent_traversal_depth, root_path)
    return resolver.resolve_file(path)
