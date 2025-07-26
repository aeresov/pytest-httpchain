import json
from pathlib import Path
from typing import Any

import jsonref
import mergedeep


class LoaderError(Exception):
    """An error parsing JSON test scenario."""


def _remove_refs(data: Any) -> Any:
    """Recursively remove all $ref keys from a data structure."""
    if isinstance(data, dict):
        return {k: _remove_refs(v) for k, v in data.items() if k != "$ref"}
    elif isinstance(data, list):
        return [_remove_refs(item) for item in data]
    else:
        return data


def load_json(path: Path) -> dict[str, Any]:
    """Load JSON test scenario file"""
    # read JSON scenario file
    try:
        test_text: str = path.read_text()
    except (PermissionError, UnicodeDecodeError) as e:
        raise LoaderError("Error reading file") from e

    # load JSON
    try:
        test_data: dict[str, Any] = json.loads(test_text)
    except json.JSONDecodeError as e:
        raise LoaderError("Invalid JSON") from e

    # process references
    try:
        ref_data: dict[str, Any] = jsonref.replace_refs(
            obj=test_data,
            base_uri=path.as_uri(),
            merge_props=False,
            lazy_load=False,
        )
    except json.JSONDecodeError as e:
        raise LoaderError("Invalid JSON in referenced files") from e
    except jsonref.JsonRefError as e:
        raise LoaderError("Invalid JSON reference") from e
    except (FileNotFoundError, PermissionError, OSError) as e:
        raise LoaderError("Error reading referenced file") from e
    except ValueError as e:
        raise LoaderError("Malformed reference") from e

    # merge
    try:
        test_data = _remove_refs(test_data)
        test_data = mergedeep.merge({}, test_data, ref_data, strategy=mergedeep.Strategy.TYPESAFE_REPLACE)
    except TypeError as e:
        raise LoaderError("Unable to merge with references") from e

    return test_data
