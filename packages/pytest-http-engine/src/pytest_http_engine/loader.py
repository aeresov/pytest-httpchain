import json
import re
from pathlib import Path
from typing import Any

from deepmerge import always_merger

# Regex pattern for parsing $ref strings with named groups
# Supports both external refs (file.json#/path) and self-refs (#/path)
REF_PATTERN = re.compile(r"^(?:(?P<file>[^#]+))?(?:#(?P<path>/.*)?)?$")


class LoaderError(Exception):
    """An error parsing JSON test scenario."""


def load_json(path: Path, merge_lists: bool = False) -> dict[str, Any]:
    """Load JSON from file and resolve all $ref statements.

    Args:
        path: Path to the JSON file to load
        merge_lists: If True, lists will be appended during merge. If False (default),
                     attempting to merge lists will raise a conflict error.

    Returns:
        Dictionary with all $ref statements resolved

    Raises:
        LoaderError: If the file cannot be loaded or parsed, or if merge conflicts occur
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        return _resolve_refs(data, path.parent, root_data=data, merge_lists=merge_lists)

    except (OSError, json.JSONDecodeError) as e:
        raise LoaderError(f"Failed to load JSON from {path}: {e}") from e


def _detect_merge_conflicts(base: Any, overlay: Any, path: str = "", merge_lists: bool = False) -> None:
    """Detect conflicts that would occur during merge.

    For this use case, any attempt to override a non-dict value is considered a conflict,
    unless merge_lists is True and both values are lists.

    Args:
        base: The base dictionary/value to merge into
        overlay: The overlay dictionary/value to merge from
        path: Current path in the data structure (for error messages)
        merge_lists: If True, lists can be merged without conflict

    Raises:
        LoaderError: If a merge conflict is detected
    """
    if isinstance(base, dict) and isinstance(overlay, dict):
        # Both are dicts, check each key in overlay
        for key, overlay_value in overlay.items():
            if key in base:
                new_path = f"{path}.{key}" if path else key
                _detect_merge_conflicts(base[key], overlay_value, new_path, merge_lists=merge_lists)
    elif isinstance(base, list) and isinstance(overlay, list) and merge_lists:
        # Both are lists and merge_lists is True - no conflict, they will be appended
        return
    elif base is not None and overlay is not None:
        # Any non-None value trying to override another non-None value is a conflict
        # (unless both are dicts or both are lists with merge_lists=True, which are handled above)
        base_type = type(base).__name__
        overlay_type = type(overlay).__name__
        path_str = f" at path '{path}'" if path else ""

        if type(base) is not type(overlay):
            # Type mismatch
            raise LoaderError(f"Merge conflict: Cannot merge {base_type} with {overlay_type}{path_str}")
        else:
            # Same type, different values - also a conflict for this use case
            raise LoaderError(f"Merge conflict: Cannot override {base_type} value{path_str}")


def _resolve_refs(data: Any, base_path: Path, context_key: str = None, root_data: Any = None, merge_lists: bool = False) -> Any:
    """Recursively resolve $ref statements while preserving sibling properties."""
    match data:
        case dict() if "$ref" in data:
            # Load and resolve referenced content
            ref_content, new_base_path = _load_ref(data["$ref"], base_path, current_data=root_data)

            # Extract sibling properties (everything except $ref)
            siblings = {k: v for k, v in data.items() if k != "$ref"}

            # Recursively resolve both referenced content and siblings
            # Use new_base_path for ref_content, original base_path for siblings
            resolved_ref = _resolve_refs(ref_content, new_base_path, root_data=root_data, merge_lists=merge_lists)
            resolved_siblings = _resolve_refs(siblings, base_path, root_data=root_data, merge_lists=merge_lists)

            # Deep merge with siblings taking precedence
            # First check for conflicts
            _detect_merge_conflicts(resolved_ref, resolved_siblings, merge_lists=merge_lists)
            # If no conflicts, perform the merge
            return always_merger.merge(resolved_ref, resolved_siblings)

        case dict():
            # No $ref, recursively process all values with context
            return {k: _resolve_refs(v, base_path, context_key=k, root_data=root_data, merge_lists=merge_lists) for k, v in data.items()}

        case list():
            return [_resolve_refs(item, base_path, root_data=root_data, merge_lists=merge_lists) for item in data]

        case _:
            return data


def _load_ref(ref: str, base_path: Path, current_data: Any = None) -> tuple[Any, Path]:
    """Load content from a $ref string (file#/json/path format).

    Args:
        ref: The $ref string to resolve
        base_path: Base path for resolving relative file references
        current_data: Current document data for self-references (starts with #)

    Returns:
        Tuple of (loaded content, new base path for further resolution)
    """
    match = REF_PATTERN.match(ref)
    if not match:
        raise LoaderError(f"Invalid $ref format: {ref}")

    file_part = match.group("file")
    json_path = match.group("path")

    # Load data from file or use current document
    if file_part:
        ref_file_path = base_path / file_part
        with open(ref_file_path, encoding="utf-8") as f:
            data = json.load(f)
        new_base_path = ref_file_path.parent
    else:
        if current_data is None:
            raise LoaderError(f"Self-reference {ref} requires current document context")
        data = current_data
        new_base_path = base_path

    # Navigate JSON path if present
    if json_path:
        for part in json_path.lstrip("/").split("/"):
            if part:
                data = data[part]

    return data, new_base_path
