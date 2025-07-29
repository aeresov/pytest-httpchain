import copy
import json
from dataclasses import dataclass
from functools import reduce
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import jsonref
from deepmerge import always_merger


class LoaderError(Exception):
    """An error parsing JSON test scenario."""


# Type alias for JSON navigation paths
JsonPath = tuple[str | int, ...]


@dataclass(frozen=True)
class RefWithProps:
    """Represents a JSON reference that has additional properties to be merged."""
    path: JsonPath  # Path to the reference (e.g., ("stages", 1, "save"))
    extra_properties: dict[str, Any]  # Properties to merge with resolved reference


def _collect_refs_with_props(obj: Any, current_path: JsonPath = ()) -> list[RefWithProps]:
    """Collect all objects that have both $ref and additional properties."""
    refs = []

    match obj:
        case dict():
            if "$ref" in obj and len(obj) > 1:
                extra_props = {k: v for k, v in obj.items() if k != "$ref"}
                refs.append(RefWithProps(current_path, extra_props))

            for key, value in obj.items():
                refs.extend(_collect_refs_with_props(value, current_path + (key,)))

        case list():
            for i, item in enumerate(obj):
                refs.extend(_collect_refs_with_props(item, current_path + (i,)))

    return refs


def _get_nested(obj: Any, path: JsonPath) -> Any:
    """Get nested value by path."""
    return reduce(lambda o, key: o[key], path, obj)


def _set_nested(obj: Any, path: JsonPath, value: Any) -> None:
    """Set nested value by path."""
    parent = _get_nested(obj, path[:-1])
    parent[path[-1]] = value


def _recursive_loader(uri: str) -> Any:
    """Load JSON file and recursively resolve any references within it."""
    with jsonref.urlopen(uri) as f:
        data = json.load(f)

    # Recursively resolve any references in the loaded data
    return jsonref.replace_refs(
        data,
        base_uri=urljoin(uri, '.'),
        merge_props=True,
        proxies=False,
        lazy_load=False,
        loader=_recursive_loader
    )


def _apply_deep_merges(data: dict[str, Any], refs_with_props: list[RefWithProps]) -> dict[str, Any]:
    """Apply deep merges for references that had additional properties."""
    # Single deepcopy upfront for the overall structure
    result = copy.deepcopy(data)

    for ref_with_props in refs_with_props:
        resolved_obj = _get_nested(result, ref_with_props.path)
        if isinstance(resolved_obj, dict):
            # Copy resolved object since always_merger.merge mutates first argument
            resolved_copy = copy.deepcopy(resolved_obj)
            merged_obj = always_merger.merge(resolved_copy, ref_with_props.extra_properties)
            _set_nested(result, ref_with_props.path, merged_obj)
    return result


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON test scenario file with recursive reference resolution and deep merging."""
    try:
        test_text = path.read_text()
        test_data = json.loads(test_text)
    except (PermissionError, UnicodeDecodeError) as e:
        raise LoaderError("Error reading file") from e
    except json.JSONDecodeError as e:
        raise LoaderError("Invalid JSON") from e

    # Collect references with extra properties before resolution
    refs_with_props = _collect_refs_with_props(test_data)

    try:
        # Resolve all references without merging extra properties
        resolved_data = jsonref.replace_refs(
            obj=test_data,
            base_uri=path.as_uri(),
            merge_props=False,
            proxies=False,
            lazy_load=False,
            loader=_recursive_loader
        )

        # Apply deep merges for references that had extra properties
        return _apply_deep_merges(resolved_data, refs_with_props)

    except json.JSONDecodeError as e:
        raise LoaderError("Invalid JSON in referenced files") from e
    except jsonref.JsonRefError as e:
        raise LoaderError("Invalid JSON reference") from e
    except (FileNotFoundError, PermissionError, OSError) as e:
        raise LoaderError("Error reading referenced file") from e
    except ValueError as e:
        raise LoaderError("Malformed reference") from e
