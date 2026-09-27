"""Equality of JSON values as JSON means it."""

from typing import Any


def json_equal(a: Any, b: Any) -> bool:
    """Equality as JSON means it: a boolean never equals a number (Python's
    ``True == 1``), an int equals the float of the same value (``1 == 1.0``),
    and arrays and objects compare element by element with the same rules.

    The one definition shared by the sibling merge (an equal value keeps, a
    different one conflicts), ``verify.jmespath`` and the validator's checks
    on it, so all three agree on what equal is."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[key], b[key]) for key in a)
    return type(a) is type(b) and a == b
