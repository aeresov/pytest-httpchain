"""Payloads shared by the unit tests of more than one module.

A plain helpers module (not conftest.py, which pytest treats as a plugin file,
not an import target)::

    from tests.unit.helpers import TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK
"""

# Deeper than any supported interpreter's JSON decoder goes: 3.13 caps C
# recursion at 10,000 levels (3,000 on Windows), and 3.14 overflows its default
# 8 MB stack well before this. The decoder raises RecursionError, which is not a
# ValueError.
TOO_DEEP_TO_PARSE = b"[" * 100_000 + b"]" * 100_000

# Parses everywhere, but Python code that walks it (pprint, jsonschema) spends
# at least one frame per level, so it overflows the default recursion limit of
# 1,000 on its own.
TOO_DEEP_TO_WALK = b"[" * 1_000 + b"]" * 1_000
