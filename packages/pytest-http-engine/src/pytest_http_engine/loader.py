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


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON from file and resolve all $ref statements.

    Args:
        path: Path to the JSON file to load

    Returns:
        Dictionary with all $ref statements resolved

    Raises:
        LoaderError: If the file cannot be loaded or parsed
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        return _resolve_refs(data, path.parent, root_data=data)

    except (OSError, json.JSONDecodeError) as e:
        raise LoaderError(f"Failed to load JSON from {path}: {e}") from e


def _resolve_refs(data: Any, base_path: Path, context_key: str = None, root_data: Any = None) -> Any:
    """Recursively resolve $ref statements while preserving sibling properties."""
    match data:
        case dict() if "$ref" in data:
            # Load and resolve referenced content
            ref_content, new_base_path = _load_ref(data["$ref"], base_path, current_data=root_data)

            # Extract sibling properties (everything except $ref)
            siblings = {k: v for k, v in data.items() if k != "$ref"}

            # Context-aware unwrapping: if we're in a context and ref has the same key, unwrap it
            if context_key and isinstance(ref_content, dict) and context_key in ref_content:
                ref_content = ref_content[context_key]

            # Recursively resolve both referenced content and siblings
            # Use new_base_path for ref_content, original base_path for siblings
            resolved_ref = _resolve_refs(ref_content, new_base_path, root_data=root_data)
            resolved_siblings = _resolve_refs(siblings, base_path, root_data=root_data)

            # Deep merge with siblings taking precedence
            return always_merger.merge(resolved_ref, resolved_siblings)

        case dict():
            # No $ref, recursively process all values with context
            return {k: _resolve_refs(v, base_path, context_key=k, root_data=root_data) for k, v in data.items()}

        case list():
            return [_resolve_refs(item, base_path, root_data=root_data) for item in data]

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
    # Parse the reference using regex
    match = REF_PATTERN.match(ref)
    if not match:
        raise LoaderError(f"Invalid $ref format: {ref}")

    file_part = match.group("file")
    json_path = match.group("path")

    # Determine data source: external file or current document (self-reference)
    if file_part:
        # External reference - load from file
        ref_file_path = base_path / file_part
        with open(ref_file_path, encoding="utf-8") as f:
            ref_data = json.load(f)
        # Update base_path to the directory of the loaded file
        new_base_path = ref_file_path.parent
    else:
        # Self-reference - use current document
        if current_data is None:
            raise LoaderError(f"Self-reference {ref} requires current document context")
        ref_data = current_data
        new_base_path = base_path

    # Navigate to specific path if provided (JSON Pointer format)
    if json_path:
        current = ref_data
        for part in json_path.lstrip("/").split("/"):
            if part:  # Skip empty parts
                current = current[part]
        return current, new_base_path
    else:
        return ref_data, new_base_path
