"""JSON file loading with reference resolution."""

from pathlib import Path
from typing import Any

from pytest_httpchain.jsonref.plumbing.reference import OpaquePredicate, ReferenceResolver


def load_json(path: Path, max_parent_traversal_depth: int = 3, root_path: Path | None = None, opaque: OpaquePredicate | None = None) -> dict[str, Any]:
    """Load a JSON file and resolve its reference directives, detecting cycles.

    ``"$schema"`` keys pass through untouched; tolerating them is the consumer's
    decision. ``opaque`` is a predicate over document positions (tuples of keys
    and indices from the root) whose subtrees pass through verbatim — the
    consumer supplies it because only it knows which positions hold foreign
    vocabulary, such as an inline JSON Schema. Positions compose across file
    boundaries: spliced-in content is judged at the reference site.

    Raises ``ReferenceResolverError`` on unreadable JSON, merge conflicts, and
    circular references.
    """
    resolver = ReferenceResolver(max_parent_traversal_depth, root_path, opaque=opaque)
    return resolver.resolve_file(path)
