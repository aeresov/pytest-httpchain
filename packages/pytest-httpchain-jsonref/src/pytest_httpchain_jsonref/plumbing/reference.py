"""Reference resolution for JSON files."""

import json
import re
from functools import reduce
from pathlib import Path
from typing import Any, Self

from deepmerge import always_merger

from pytest_httpchain_jsonref.exceptions import ReferenceResolverError
from pytest_httpchain_jsonref.plumbing.circular import CircularDependencyTracker
from pytest_httpchain_jsonref.plumbing.path import PathValidator

REF_PATTERN = re.compile(r"^(?P<file>[^#]+)?(?:#(?P<pointer>/.*))?$")


class ReferenceResolver:
    """Resolves JSON references ($ref) in documents."""

    def __init__(self, max_parent_traversal_depth: int = 3):
        self.max_parent_traversal_depth = max_parent_traversal_depth
        self.path_validator = PathValidator()
        self.tracker = CircularDependencyTracker()
        self.base_path: Path | None = None
        self.root_path: Path | None = None
        self._cache: dict[tuple[Path, str], Any] = {}

    def resolve_document(self, data: dict[str, Any], base_path: Path) -> dict[str, Any]:
        """Resolve all references in a document.

        Args:
            data: The document data to resolve references in
            base_path: The base path for resolving relative references

        Returns:
            The document with all references resolved

        Raises:
            ReferenceResolverError: If resolution fails
        """
        self.base_path = base_path
        return self._resolve_refs(data, base_path, root_data=data)

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
            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            self.root_path = path.parent
            return self.resolve_document(data, path.parent)

        except (OSError, json.JSONDecodeError) as e:
            raise ReferenceResolverError(f"Failed to load JSON from {path}: {e}") from e

    def _resolve_refs(
        self,
        data: Any,
        current_path: Path,
        root_data: Any,
    ) -> Any:
        match data:
            case dict() if "$ref" in data:
                return self._resolve_single_ref(data, current_path, root_data)
            case dict():
                return {key: self._resolve_refs(value, current_path, root_data) for key, value in data.items()}
            case list():
                return [self._resolve_refs(item, current_path, root_data) for item in data]
            case _:
                return data

    def _resolve_single_ref(
        self,
        data: dict[str, Any],
        current_path: Path,
        root_data: Any,
    ) -> Any:
        ref_value = data["$ref"]
        match = REF_PATTERN.match(ref_value)

        if not match:
            raise ReferenceResolverError(f"Invalid $ref format: {ref_value}")

        file_path = match.group("file")
        pointer = match.group("pointer") or ""

        if file_path:
            referenced_data = self._resolve_external_ref(file_path, pointer, current_path)
        else:
            referenced_data = self._resolve_internal_ref(pointer, root_data)

        return self._merge_with_siblings(data, referenced_data, current_path, root_data)

    def _resolve_external_ref(
        self,
        file_path: str,
        pointer: str,
        current_path: Path,
    ) -> Any:
        resolved_path = self.path_validator.validate_ref_path(file_path, current_path, self.root_path or current_path, self.max_parent_traversal_depth)

        cache_key = (resolved_path, pointer)
        if cache_key in self._cache:
            return self._cache[cache_key]

        self.tracker.check_external_ref(resolved_path, pointer)

        try:
            full_external_data = self._load_json_file(resolved_path)
            external_data = self._navigate_pointer(full_external_data, pointer) if pointer else full_external_data

            child_resolver = self._create_child_resolver()
            result = child_resolver._resolve_refs(external_data, resolved_path.parent, root_data=full_external_data)
            self._cache[cache_key] = result
            return result

        except (OSError, json.JSONDecodeError) as e:
            raise ReferenceResolverError(f"Failed to load external reference {file_path}: {e}") from e
        finally:
            self.tracker.clear_external_ref(resolved_path, pointer)

    def _resolve_internal_ref(
        self,
        pointer: str,
        root_data: Any,
    ) -> Any:
        self.tracker.check_internal_ref(pointer)

        try:
            referenced_data = self._navigate_pointer(root_data, pointer)
            return self._resolve_refs(referenced_data, self.base_path, root_data)
        finally:
            self.tracker.clear_internal_ref(pointer)

    def _navigate_pointer(self, data: Any, pointer: str) -> Any:
        if not pointer:
            return data

        parts = self.path_validator.parse_json_pointer(pointer)

        try:
            return reduce(lambda obj, key: obj[int(key)] if isinstance(obj, list) else obj[key], parts, data)
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise ReferenceResolverError(f"Invalid JSON pointer {pointer}: {e}") from e

    def _merge_with_siblings(
        self,
        ref_dict: dict[str, Any],
        referenced_data: Any,
        current_path: Path,
        root_data: Any,
    ) -> Any:
        siblings = {k: v for k, v in ref_dict.items() if k != "$ref"}

        if not siblings:
            return referenced_data

        if not isinstance(referenced_data, dict):
            if len(siblings) > 0:
                raise ReferenceResolverError("Cannot merge non-dict reference with sibling properties")
            return referenced_data

        resolved_siblings = self._resolve_refs(siblings, current_path, root_data)

        self._detect_merge_conflicts(referenced_data, resolved_siblings)

        return always_merger.merge(referenced_data, resolved_siblings)

    def _load_json_file(self, path: Path) -> dict[str, Any]:
        """Load and cache JSON file content."""
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _create_child_resolver(self) -> Self:
        """Create a child resolver with inherited state."""
        child_resolver = type(self)(self.max_parent_traversal_depth)
        child_resolver.tracker = self.tracker.create_child_tracker()
        child_resolver.root_path = self.root_path
        child_resolver._cache = self._cache.copy()
        return child_resolver

    def _detect_merge_conflicts(
        self,
        base: Any,
        overlay: Any,
        path: str = "",
    ) -> None:
        if base is None or overlay is None or base == overlay:
            return

        if isinstance(base, dict) and isinstance(overlay, dict):
            for key, value in overlay.items():
                if key in base:
                    new_path = f"{path}.{key}" if path else key
                    self._detect_merge_conflicts(base[key], value, new_path)
            return

        if isinstance(base, list) and isinstance(overlay, list):
            return

        raise ReferenceResolverError(f"Merge conflict at {path if path else 'root'}")
