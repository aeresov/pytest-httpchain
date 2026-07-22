"""Reference resolution for JSON files."""

import json
import re
from collections.abc import Callable
from functools import reduce
from pathlib import Path
from typing import Any, Self

from deepmerge import STRATEGY_END, Merger

from pytest_httpchain.jsonref.exceptions import DuplicateKeyError, ReferenceResolverError
from pytest_httpchain.jsonref.plumbing.circular import CircularDependencyTracker
from pytest_httpchain.jsonref.plumbing.path import RefPathHelper

# Predicate over a document position — the tuple of dict keys / list indices
# from the document root to a value. Positions are composed across file
# boundaries: content spliced in via a reference is judged at the reference
# site's position plus its fragment-relative path.
OpaquePredicate = Callable[[tuple[str | int, ...]], bool]

REF_PATTERN = re.compile(r"^(?P<file>[^#]+)?(?:#(?P<pointer>/.*))?$")

# Supported reference keys: $include/$merge (preferred, avoids VS Code conflicts) and $ref (legacy)
REF_KEYS = ("$include", "$merge", "$ref")


def _raise_on_conflict(config: Any, path: list[Any], base: Any, nxt: Any) -> Any:
    """deepmerge strategy: allow equal values, raise on any real conflict.

    Serves as both the fallback strategy (same-type values that aren't dicts
    or lists) and the type-conflict strategy, so the no-last-wins promise
    holds for every type combination — including a null on either side (null
    is a value, not an override or a hole).
    """
    if base == nxt:
        return base
    location = ".".join(str(part) for part in path) or "root"
    raise ReferenceResolverError(f"Merge conflict at {location}")


# The single encoding of the sibling-merge policy: nested dicts merge
# recursively, lists concatenate, and any other overlap must be equal —
# otherwise it is a conflict and resolution fails loudly.
_SIBLING_MERGER = Merger(
    [(list, "append"), (dict, "merge")],
    [_raise_on_conflict],
    [_raise_on_conflict],
)


def _build_opaque_aware_merger(opaque: "OpaquePredicate", base_path: tuple[str | int, ...]) -> Merger:
    """Sibling merger that treats opaque positions as atomic values.

    An opaque subtree is verbatim foreign vocabulary, so the recursive dict
    merge must not blend two of them into one: at an opaque position the
    whole values must be equal (kept) or it is a merge conflict — the same
    no-silent-contradiction rule scalars already follow. ``base_path`` is the
    reference site's document position; deepmerge's per-strategy ``path`` is
    relative to the merge root, so their concatenation is the absolute
    position the ``opaque`` predicate expects.
    """

    def atomic_at_opaque(config: Any, path: list[Any], base: Any, nxt: Any) -> Any:
        if opaque(base_path + tuple(path)):
            return _raise_on_conflict(config, path, base, nxt)
        return STRATEGY_END

    return Merger(
        [(list, [atomic_at_opaque, "append"]), (dict, [atomic_at_opaque, "merge"])],
        [_raise_on_conflict],
        [_raise_on_conflict],
    )


def _parse_json_rejecting_duplicates(path: Path) -> Any:
    """Parse a JSON file, rejecting duplicate object keys.

    Plain ``json.loads`` silently keeps the LAST duplicate key — in scenario
    terms that silently deletes a step (e.g. a duplicated response-step key
    drops a verify) and weakens the test with no diagnostic. Scenario files
    are code; a duplicate key is an author error worth failing on.

    Raised as ``DuplicateKeyError`` (with the offending key and file) in
    this tight scope so it propagates through the callers' narrower
    ``except (OSError, json.JSONDecodeError)`` blocks unwrapped.
    """

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateKeyError(f"Duplicate key '{key}' in JSON object in {path}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs_hook)


class ReferenceResolver:
    """Resolves JSON reference directives ($include/$merge, or legacy $ref) in documents.

    ``opaque`` (optional) marks document positions whose values are not the
    resolver's to process: a subtree at a position the predicate matches is
    passed through verbatim — no directive resolution, no merging — even when
    it contains ``$ref``/``$include``/``$merge`` keys. The consumer supplies
    the predicate because only it knows which positions hold foreign
    vocabulary (e.g. inline JSON Schemas, where ``$ref`` belongs to the
    schema validator).
    """

    def __init__(self, max_parent_traversal_depth: int = 3, root_path: Path | None = None, opaque: OpaquePredicate | None = None):
        self.max_parent_traversal_depth = max_parent_traversal_depth
        self.ref_paths = RefPathHelper()
        self.tracker = CircularDependencyTracker()
        self.base_path: Path | None = None
        self.root_path = root_path
        self.opaque = opaque

    def resolve_document(self, data: dict[str, Any], base_path: Path, root_path: Path | None = None) -> dict[str, Any]:
        """Resolve all references in a document.

        Args:
            data: The document data to resolve references in
            base_path: The base path for resolving relative references
            root_path: The root path references must not escape. Defaults to
                self.root_path (or base_path) when not supplied.

        Returns:
            The document with all references resolved

        Raises:
            ReferenceResolverError: If resolution fails
        """
        self.base_path = base_path
        effective_root = root_path if root_path is not None else self.root_path
        return self._resolve_refs(data, base_path, root_data=data, root_path=effective_root or base_path, doc_path=())

    def resolve_file(self, path: Path) -> dict[str, Any]:
        """Load a JSON file and resolve all references.

        Args:
            path: Path to the JSON file to load

        Returns:
            The loaded document with all references resolved

        Raises:
            ReferenceResolverError: If the file cannot be loaded or references cannot be resolved
        """
        try:
            data = _parse_json_rejecting_duplicates(path)

            # Compute the effective root locally rather than mutating self.root_path,
            # so the resolver is not single-use (a second call would otherwise be
            # validated against the first file's derived root).
            root_path = self.root_path
            if not root_path:
                # If root_path wasn't provided, find a suitable one by going up the
                # directory tree up to max_parent_traversal_depth levels.
                root_path = path.parent
                for _ in range(self.max_parent_traversal_depth):
                    parent = root_path.parent
                    if parent == root_path:
                        break  # Reached filesystem root
                    root_path = parent

            return self.resolve_document(data, path.parent, root_path)

        except (OSError, json.JSONDecodeError) as e:
            raise ReferenceResolverError(f"Failed to load JSON from {path}: {e}") from e

    def _resolve_refs(
        self,
        data: Any,
        current_path: Path,
        root_data: Any,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        if self.opaque is not None and self.opaque(doc_path):
            return data
        match data:
            case dict() if self._get_ref_key(data):
                return self._resolve_single_ref(data, current_path, root_data, root_path, doc_path)
            case dict():
                return {key: self._resolve_refs(value, current_path, root_data, root_path, doc_path + (key,)) for key, value in data.items()}
            case list():
                return [self._resolve_refs(item, current_path, root_data, root_path, doc_path + (index,)) for index, item in enumerate(data)]
            case _:
                return data

    def _get_ref_key(self, data: dict[str, Any]) -> str | None:
        """Get the reference key ($include/$merge/$ref) if present in data.

        Raises:
            ReferenceResolverError: If more than one directive key is present.
        """
        present = [key for key in REF_KEYS if key in data]
        if len(present) > 1:
            raise ReferenceResolverError(f"Multiple reference directives in one object: {', '.join(present)}")
        return present[0] if present else None

    def _resolve_single_ref(
        self,
        data: dict[str, Any],
        current_path: Path,
        root_data: Any,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        ref_key = self._get_ref_key(data)
        assert ref_key is not None
        ref_value = data[ref_key]
        if not isinstance(ref_value, str):
            raise ReferenceResolverError(f"{ref_key} value must be a string, got {type(ref_value).__name__}: {ref_value!r}")
        match = REF_PATTERN.match(ref_value)

        if not match:
            raise ReferenceResolverError(f"Invalid {ref_key} format: {ref_value}")

        file_path = match.group("file")
        pointer = match.group("pointer") or ""

        # The referenced content lands at the reference site, so it is resolved
        # at the site's document position (doc_path), not its source position.
        if file_path:
            referenced_data = self._resolve_external_ref(file_path, pointer, current_path, root_path, doc_path)
        else:
            referenced_data = self._resolve_internal_ref(pointer, root_data, root_path, doc_path)

        return self._merge_with_siblings(data, referenced_data, current_path, root_data, root_path, doc_path)

    def _resolve_external_ref(
        self,
        file_path: str,
        pointer: str,
        current_path: Path,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        resolved_path = self.ref_paths.validate_ref_path(file_path, current_path, root_path, self.max_parent_traversal_depth)

        self.tracker.check_external_ref(resolved_path, pointer)

        try:
            full_external_data = self._load_json_file(resolved_path)
            external_data = self._navigate_pointer(full_external_data, pointer) if pointer else full_external_data

            child_resolver = self._create_child_resolver(root_path)
            child_resolver.base_path = resolved_path.parent
            result = child_resolver._resolve_refs(external_data, resolved_path.parent, root_data=full_external_data, root_path=root_path, doc_path=doc_path)
            return result

        except (OSError, json.JSONDecodeError) as e:
            raise ReferenceResolverError(f"Failed to load external reference {file_path}: {e}") from e
        finally:
            self.tracker.clear_external_ref(resolved_path, pointer)

    def _resolve_internal_ref(
        self,
        pointer: str,
        root_data: Any,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        self.tracker.check_internal_ref(pointer)

        try:
            referenced_data = self._navigate_pointer(root_data, pointer)
            assert self.base_path is not None
            return self._resolve_refs(referenced_data, self.base_path, root_data, root_path, doc_path)
        finally:
            self.tracker.clear_internal_ref(pointer)

    def _navigate_pointer(self, data: Any, pointer: str) -> Any:
        if not pointer:
            return data

        parts = self.ref_paths.parse_json_pointer(pointer)

        def navigate_step(obj: Any, key: str) -> Any:
            if isinstance(obj, list):
                # RFC 6901: array indices must not have leading zeros (except "0" itself)
                if len(key) > 1 and key.startswith("0"):
                    raise ValueError(f"Array index '{key}' has leading zeros")
                return obj[int(key)]
            return obj[key]

        try:
            return reduce(navigate_step, parts, data)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ReferenceResolverError(f"Invalid JSON pointer {pointer}: {e}") from e

    def _merge_with_siblings(
        self,
        ref_dict: dict[str, Any],
        referenced_data: Any,
        current_path: Path,
        root_data: Any,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        siblings = {k: v for k, v in ref_dict.items() if k not in REF_KEYS}

        if not siblings:
            return referenced_data

        # siblings is non-empty here (early return above), so a non-dict
        # referenced value can never be merged with them.
        if not isinstance(referenced_data, dict):
            raise ReferenceResolverError("Cannot merge non-dict reference with sibling properties")

        resolved_siblings = self._resolve_refs(siblings, current_path, root_data, root_path, doc_path)

        merger = _SIBLING_MERGER if self.opaque is None else _build_opaque_aware_merger(self.opaque, doc_path)
        return merger.merge(referenced_data, resolved_siblings)

    def _load_json_file(self, path: Path) -> dict[str, Any]:
        """Load JSON file content."""
        return _parse_json_rejecting_duplicates(path)

    def _create_child_resolver(self, root_path: Path) -> Self:
        """Create a child resolver with inherited state."""
        child_resolver = type(self)(self.max_parent_traversal_depth, root_path, opaque=self.opaque)
        child_resolver.tracker = self.tracker.create_child_tracker()
        return child_resolver
