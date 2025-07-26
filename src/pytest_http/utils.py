from typing import Any


def remove_refs(data: Any) -> Any:
    """Recursively remove all $ref keys from a data structure."""
    if isinstance(data, dict):
        return {k: remove_refs(v) for k, v in data.items() if k != "$ref"}
    elif isinstance(data, list):
        return [remove_refs(item) for item in data]
    else:
        return data
