"""JSON file loading with reference resolution."""

from pathlib import Path
from typing import Any

from pytest_httpchain.jsonref.plumbing.reference import PositionPredicate, ReferenceResolver


def load_json(
    path: Path,
    max_parent_traversal_depth: int = 3,
    root_path: Path | None = None,
    opaque: PositionPredicate | None = None,
    atomic: PositionPredicate | None = None,
) -> dict[str, Any]:
    """Load a JSON file and resolve its reference directives, detecting cycles.

    ``"$schema"`` keys pass through untouched; tolerating them is the consumer's
    decision. ``opaque`` is a predicate over document positions (tuples of keys
    and indices from the root) whose subtrees pass through verbatim — the
    consumer supplies it because only it knows which positions hold foreign
    vocabulary, such as an inline JSON Schema. ``atomic`` is a predicate over
    positions whose value merges with a sibling as a whole: equal values keep,
    differing ones conflict, where a list would otherwise be concatenated —
    for a list whose entries are alternatives, which concatenation would widen.
    Opaque positions merge that way too. Positions compose across file
    boundaries: spliced-in content is judged at the reference site.

    Raises ``ReferenceResolverError`` on unreadable, malformed, non-UTF-8 or too
    deeply nested JSON (chaining the original error as ``__cause__``), merge
    conflicts, and circular references.
    """
    resolver = ReferenceResolver(max_parent_traversal_depth, root_path, opaque=opaque, atomic=atomic)
    return resolver.resolve_file(path)
