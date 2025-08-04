import json
import re
from functools import reduce
from pathlib import Path
from typing import Any

from deepmerge import always_merger

# Regex pattern for parsing $ref values
# External file: "file.json"
# External reference: "file.json#/path/to/node"
# Internal pointer: "#/path/to/node"
REF_PATTERN = re.compile(r"^(?P<file>[^#]+)?(?:#(?P<pointer>/.*))?$")

# Maximum number of parent directory traversals allowed in $ref paths
MAX_PARENT_TRAVERSAL_DEPTH = 3


class LoaderError(Exception):
    """An error parsing JSON test scenario."""


def load_json(path: Path, merge_lists: bool = False, max_parent_traversal_depth: int = MAX_PARENT_TRAVERSAL_DEPTH) -> dict[str, Any]:
    """Load JSON from file and resolve all $ref statements with circular reference protection.

    Args:
        path: Path to the JSON file to load
        merge_lists: If True, lists will be appended during merge. If False (default),
                     attempting to merge lists will raise a conflict error.
        max_parent_traversal_depth: Maximum number of parent directory traversals allowed in $ref paths

    Returns:
        Dictionary with all $ref statements resolved

    Raises:
        LoaderError: If the file cannot be loaded or parsed, if merge conflicts occur,
                     or if circular references are detected
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        external_refs = set()
        internal_refs = set()
        return _resolve_refs(
            data,
            path.parent,
            root_data=data,
            merge_lists=merge_lists,
            external_refs=external_refs,
            internal_refs=internal_refs,
            max_parent_traversal_depth=max_parent_traversal_depth,
        )

    except (OSError, json.JSONDecodeError) as e:
        raise LoaderError(f"Failed to load JSON from {path}: {e}") from e


def _detect_merge_conflicts(
    base: Any,
    overlay: Any,
    path: str = "",
    merge_lists: bool = False,
) -> None:
    """Detect conflicts during merge.

    Args:
        base: The base dictionary/value to merge into
        overlay: The overlay dictionary/value to merge from
        path: Current path in the data structure (for error messages)
        merge_lists: If True, lists can be merged without conflict

    Raises:
        LoaderError: If a merge conflict is detected
    """
    if base is None or overlay is None:
        return

    if isinstance(base, dict) and isinstance(overlay, dict):
        for key, value in overlay.items():
            if key in base:
                new_path = f"{path}.{key}" if path else key
                _detect_merge_conflicts(base[key], value, new_path, merge_lists)
        return

    if isinstance(base, list) and isinstance(overlay, list) and merge_lists:
        return

    # Allow merging of identical values
    if base == overlay:
        return

    raise LoaderError(f"Merge conflict at {path if path else 'root'}")


def _resolve_refs(
    data: Any,
    base_path: Path,
    root_data: Any,
    merge_lists: bool,
    external_refs: set,
    internal_refs: set,
    max_parent_traversal_depth: int,
) -> Any:
    """Recursively resolve $ref statements while preserving sibling properties and preventing circular references.

    Args:
        data: The data structure to process
        base_path: Base path for resolving relative file references
        root_data: Root document data for self-references
        merge_lists: Whether to merge lists or raise conflict error
        external_refs: Set of visited file references to detect circular dependencies
        internal_refs: Set of visited internal JSON pointers to detect circular dependencies
        max_parent_traversal_depth: Maximum number of parent directory traversals allowed

    Returns:
        Data with all $ref statements resolved

    Raises:
        LoaderError: If circular references are detected
    """

    match data:
        case {"$ref": ref_value, **siblings}:
            # Parse the reference using regex
            match = REF_PATTERN.match(ref_value)
            if not match:
                raise LoaderError(f"Invalid $ref format: {ref_value}")

            file_part = match.group("file")

            # Determine reference type and check for circular dependencies
            if file_part:
                # File reference (with or without pointer)
                # Use the full reference string as the key to allow different pointers to the same file
                ref_key = str((base_path / file_part).resolve()) + (f"#{match.group('pointer')}" if match.group("pointer") else "")

                # Check for circular file reference
                if ref_key in external_refs:
                    raise LoaderError(f"Circular reference detected: {ref_value}")

                # Add to visited set
                external_refs.add(ref_key)
                is_file_ref = True
            else:
                # Pure internal JSON pointer reference (starts with #)
                ref_key = ref_value

                # Check for circular internal reference
                if ref_key in internal_refs:
                    raise LoaderError(f"Circular internal reference detected: {ref_value}")

                # Add to visited set
                internal_refs.add(ref_key)
                is_file_ref = False

            try:
                # Pass None as current_data if this is a file reference, otherwise pass root_data
                current_data_for_ref = None if is_file_ref else root_data
                ref_content, new_base_path, full_document = _load_ref(ref_value, base_path, current_data_for_ref, max_parent_traversal_depth)
                # When loading a file reference, the full document becomes the new root for self-references
                new_root_data = full_document if is_file_ref else root_data
                resolved_ref = _resolve_refs(ref_content, new_base_path, new_root_data, merge_lists, external_refs, internal_refs, max_parent_traversal_depth)
                resolved_siblings = _resolve_refs(siblings, base_path, root_data, merge_lists, external_refs, internal_refs, max_parent_traversal_depth)
                _detect_merge_conflicts(resolved_ref, resolved_siblings, merge_lists=merge_lists)
                return always_merger.merge(resolved_ref, resolved_siblings)
            finally:
                # Remove from visited set after processing to allow the same ref in different branches
                if is_file_ref:
                    external_refs.remove(ref_key)
                else:
                    internal_refs.remove(ref_key)

        case dict():
            return {k: _resolve_refs(v, base_path, root_data, merge_lists, external_refs, internal_refs, max_parent_traversal_depth) for k, v in data.items()}
        case list():
            return [_resolve_refs(item, base_path, root_data, merge_lists, external_refs, internal_refs, max_parent_traversal_depth) for item in data]
        case _:
            return data


def _load_ref(ref: str, base_path: Path, current_data: Any = None, max_parent_traversal_depth: int = MAX_PARENT_TRAVERSAL_DEPTH) -> tuple[Any, Path, Any]:
    """Load content from a $ref string (file#/json/path format) with security validation.

    Args:
        ref: The $ref string to resolve
        base_path: Base path for resolving relative file references
        current_data: Current document data for self-references (starts with #)
        max_parent_traversal_depth: Maximum number of parent directory traversals allowed

    Returns:
        Tuple of (loaded content, new base path for further resolution, full document for self-references)

    Raises:
        LoaderError: If path validation fails or file cannot be loaded
    """
    # Parse the reference using regex
    match = REF_PATTERN.match(ref)
    if not match:
        raise LoaderError(f"Invalid $ref format: {ref}")

    file_part = match.group("file")
    pointer_part = match.group("pointer")

    if file_part:
        ref_file_path = base_path / file_part

        # Validate path to prevent directory traversal
        try:
            # Resolve to absolute path
            resolved_path = ref_file_path.resolve()
            base_path_resolved = base_path.resolve()

            # Count how many levels up from base_path to resolved_path
            try:
                # If resolved_path is under base_path, this will work
                resolved_path.relative_to(base_path_resolved)
                # File is in same directory or subdirectory - always allowed
            except ValueError:
                # File is outside base_path - count ".." in the original path
                parent_traversals = 0
                path_parts = file_part.split("/")

                for part in path_parts:
                    if part == "..":
                        parent_traversals += 1
                    elif part and part != ".":
                        # Any non-parent directory reference after going up
                        # doesn't reduce the parent traversal count
                        pass

                if parent_traversals > max_parent_traversal_depth:
                    raise LoaderError(
                        f"Path traversal exceeds maximum depth ({max_parent_traversal_depth}): {file_part} contains {parent_traversals} parent directory references"
                    ) from None

        except (ValueError, RuntimeError) as e:
            raise LoaderError(f"Invalid path in $ref: {file_part}") from e

        try:
            with open(resolved_path, encoding="utf-8") as f:
                full_document = json.load(f)
            data = full_document
            new_base_path = resolved_path.parent
        except (OSError, json.JSONDecodeError) as e:
            raise LoaderError(f"Failed to load referenced file {resolved_path}: {e}") from e
    else:
        if current_data is None:
            raise LoaderError(f"Self-reference {ref} requires current document context")
        data = current_data
        full_document = current_data
        new_base_path = base_path

    # Extract the pointed-to content if there's a JSON pointer
    if pointer_part:
        try:
            parts = [p for p in pointer_part.split("/") if p]
            data = reduce(lambda d, key: d[key], parts, data)
        except (KeyError, TypeError, IndexError) as e:
            raise LoaderError(f"Invalid JSON pointer in $ref: {pointer_part}") from e

    # Return extracted content, base path, and full document
    return data, new_base_path, full_document
