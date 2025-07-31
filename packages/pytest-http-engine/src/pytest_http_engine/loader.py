import json
from functools import reduce
from pathlib import Path
from typing import Any

from deepmerge import always_merger


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

    raise LoaderError(f"Merge conflict at {path if path else 'root'}")


def _resolve_refs(data: Any, base_path: Path, root_data: Any = None, merge_lists: bool = False) -> Any:
    """Recursively resolve $ref statements while preserving sibling properties."""
    match data:
        case {"$ref": ref_value, **siblings}:
            ref_content, new_base_path = _load_ref(ref_value, base_path, root_data)
            resolved_ref = _resolve_refs(ref_content, new_base_path, root_data, merge_lists)
            resolved_siblings = _resolve_refs(siblings, base_path, root_data, merge_lists)
            _detect_merge_conflicts(resolved_ref, resolved_siblings, merge_lists=merge_lists)
            return always_merger.merge(resolved_ref, resolved_siblings)
        case dict():
            return {k: _resolve_refs(v, base_path, root_data, merge_lists) for k, v in data.items()}
        case list():
            return [_resolve_refs(item, base_path, root_data, merge_lists) for item in data]
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
    file_part, _, path_part = ref.partition("#")
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

    if path_part:
        parts = [p for p in path_part.split("/") if p]
        data = reduce(lambda d, key: d[key], parts, data)

    return data, new_base_path
