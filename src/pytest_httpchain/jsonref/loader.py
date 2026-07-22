"""JSON file loading with reference resolution."""

from pathlib import Path
from typing import Any

from pytest_httpchain.jsonref.plumbing.reference import OpaquePredicate, ReferenceResolver


def load_json(path: Path, max_parent_traversal_depth: int = 3, root_path: Path | None = None, opaque: OpaquePredicate | None = None) -> dict[str, Any]:
    """Load JSON from file and resolve all $include/$merge/$ref statements with circular reference protection.

    All three directives ($include, $merge, $ref) work identically. $include and $merge are preferred
    as they avoid conflicts with VS Code's JSON Schema validation (which treats $ref specially).

    "$schema" keys pass through untouched — whether and where to tolerate them
    is the consumer's decision (pytest-httpchain's models drop them during validation).

    Args:
        path: Path to the JSON file to load
        max_parent_traversal_depth: Maximum number of parent directory traversals allowed in reference paths
        root_path: Optional root directory for resolving references (e.g., pytest's rootdir)
        opaque: Optional predicate over document positions (tuples of dict
            keys / list indices from the root). A subtree at a matching
            position is passed through verbatim — no directive resolution, no
            merging — even when it contains ``$ref``/``$include``/``$merge``
            keys. Positions compose across file boundaries: content spliced in
            via a reference is judged at the reference site's position plus
            its fragment-relative path. The consumer supplies the predicate
            because only it knows which positions hold foreign vocabulary
            (e.g. inline JSON Schemas, where ``$ref`` belongs to the schema
            validator).

    Returns:
        Dictionary with all $include/$ref statements resolved

    Raises:
        ReferenceResolverError: If the file cannot be loaded or parsed, if merge conflicts occur, or if circular references are detected
    """
    resolver = ReferenceResolver(max_parent_traversal_depth, root_path, opaque=opaque)
    return resolver.resolve_file(path)
