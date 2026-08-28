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
from pytest_httpchain.jsonref.plumbing.path import parse_json_pointer, validate_ref_path

# Predicate over a document position: the tuple of keys and indices from the
# root to a value, composed across file boundaries.
type OpaquePredicate = Callable[[tuple[str | int, ...]], bool]

REF_PATTERN = re.compile(r"^(?P<file>[^#]+)?(?:#(?P<pointer>/.*))?$")

REF_KEYS = ("$include", "$merge", "$ref")


def _raise_on_conflict(config: Any, path: list[Any], base: Any, nxt: Any) -> Any:
    """deepmerge strategy: keep equal values, raise on any real conflict.

    Used as both the fallback and the type-conflict strategy, so no-last-wins
    holds for every combination, nulls included. Equality is judged in JSON
    terms, where Python's ``True == 1`` must not pass as equal.
    """
    if isinstance(base, bool) == isinstance(nxt, bool) and base == nxt:
        return base
    location = ".".join(str(part) for part in path) or "root"
    raise ReferenceResolverError(f"Merge conflict at {location}")


# The sibling-merge policy: dicts merge recursively, lists concatenate, and any
# other overlap must be equal.
_SIBLING_MERGER = Merger(
    [(list, "append"), (dict, "merge")],
    [_raise_on_conflict],
    [_raise_on_conflict],
)


def _build_opaque_aware_merger(opaque: "OpaquePredicate", base_path: tuple[str | int, ...]) -> Merger:
    """Sibling merger treating opaque positions as atomic: two opaque subtrees
    must be equal or conflict, never blend.

    ``base_path`` is the reference site's position, since deepmerge's ``path`` is
    relative to the merge root.
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

    ``json.loads`` keeps the last one, which in a scenario silently drops a step
    and weakens the test. `DuplicateKeyError` propagates unwrapped through the
    callers' narrower except blocks.
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
    """Resolves reference directives in a document.

    ``opaque`` marks positions that are not the resolver's to process: those
    subtrees pass through verbatim, directives and all.
    """

    def __init__(self, max_parent_traversal_depth: int = 3, root_path: Path | None = None, opaque: OpaquePredicate | None = None):
        self.max_parent_traversal_depth = max_parent_traversal_depth
        self.tracker = CircularDependencyTracker()
        self.root_path = root_path
        self.opaque = opaque

    def resolve_document(self, data: dict[str, Any], base_path: Path, root_path: Path) -> dict[str, Any]:
        """Resolve every reference in a document, relative to ``base_path`` and
        sandboxed under ``root_path``."""
        return self._resolve_refs(data, base_path, root_data=data, root_path=root_path, doc_path=())

    def resolve_file(self, path: Path) -> dict[str, Any]:
        """Load a JSON file and resolve its references."""
        try:
            data = _parse_json_rejecting_duplicates(path)

            # Derived locally, not stored: mutating self.root_path would validate
            # a second call against the first file's root.
            root_path = self.root_path
            if not root_path:
                root_path = path.parent
                for _ in range(self.max_parent_traversal_depth):
                    parent = root_path.parent
                    if parent == root_path:
                        break
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
        """The reference key present in ``data``, or None; several is an error."""
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

        # The content lands at the reference site, so it resolves at the site's
        # position, not its source's.
        if file_path:
            referenced_data = self._resolve_external_ref(file_path, pointer, current_path, root_path, doc_path)
        else:
            referenced_data = self._resolve_internal_ref(pointer, current_path, root_data, root_path, doc_path)

        return self._merge_with_siblings(data, referenced_data, current_path, root_data, root_path, doc_path)

    def _resolve_external_ref(
        self,
        file_path: str,
        pointer: str,
        current_path: Path,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        resolved_path = validate_ref_path(file_path, current_path, root_path, self.max_parent_traversal_depth)

        self.tracker.check_external_ref(resolved_path, pointer)

        try:
            full_external_data = _parse_json_rejecting_duplicates(resolved_path)
            external_data = self._navigate_pointer(full_external_data, pointer, source=resolved_path) if pointer else full_external_data

            child_resolver = self._create_child_resolver(root_path)
            return child_resolver._resolve_refs(external_data, resolved_path.parent, root_data=full_external_data, root_path=root_path, doc_path=doc_path)

        except (OSError, json.JSONDecodeError) as e:
            raise ReferenceResolverError(f"Failed to load external reference {file_path}: {e}") from e
        finally:
            self.tracker.clear_external_ref(resolved_path, pointer)

    def _resolve_internal_ref(
        self,
        pointer: str,
        current_path: Path,
        root_data: Any,
        root_path: Path,
        doc_path: tuple[str | int, ...],
    ) -> Any:
        self.tracker.check_internal_ref(pointer)

        try:
            referenced_data = self._navigate_pointer(root_data, pointer)
            return self._resolve_refs(referenced_data, current_path, root_data, root_path, doc_path)
        finally:
            self.tracker.clear_internal_ref(pointer)

    def _navigate_pointer(self, data: Any, pointer: str, source: Path | None = None) -> Any:
        if not pointer:
            return data

        parts = parse_json_pointer(pointer)

        def navigate_step(obj: Any, key: str) -> Any:
            if isinstance(obj, list):
                # RFC 6901 indices are digit-only without leading zeros; int()
                # would accept "-1" and silently index from the wrong end.
                if not (key.isascii() and key.isdigit()):
                    raise ValueError(f"Array index '{key}' is not a valid RFC 6901 index")
                if len(key) > 1 and key.startswith("0"):
                    raise ValueError(f"Array index '{key}' has leading zeros")
                return obj[int(key)]
            return obj[key]

        try:
            return reduce(navigate_step, parts, data)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            where = f" in {source}" if source is not None else ""
            raise ReferenceResolverError(f"Invalid JSON pointer {pointer}{where}: {e}") from e

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

        if not isinstance(referenced_data, dict):
            raise ReferenceResolverError("Cannot merge non-dict reference with sibling properties")

        resolved_siblings = self._resolve_refs(siblings, current_path, root_data, root_path, doc_path)

        merger = _SIBLING_MERGER if self.opaque is None else _build_opaque_aware_merger(self.opaque, doc_path)
        return merger.merge(referenced_data, resolved_siblings)

    def _create_child_resolver(self, root_path: Path) -> Self:
        """A resolver for another document, inheriting the cycle tracker."""
        child_resolver = type(self)(self.max_parent_traversal_depth, root_path, opaque=self.opaque)
        child_resolver.tracker = self.tracker.create_child_tracker()
        return child_resolver
