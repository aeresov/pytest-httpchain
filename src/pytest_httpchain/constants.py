"""Shared constants for the pytest-httpchain plugin.

Configuration option names (settable in pytest.ini / pyproject.toml) and the
user-function name grammar shared by the models' validator and the userfunc
importer.
"""

import re
from enum import StrEnum

# Matches "module.path:function_name". The module path is REQUIRED and must be a
# well-formed dotted path: identifier segments joined by single dots, with no
# leading, trailing, or doubled dots (so "a..b:f" and "mod.:f" are rejected at
# validation instead of failing later at import time). The function part is a
# single identifier. This is the single grammar shared by the models'
# FunctionImportName validator and userfunc's importer, so a bare name (no
# module) fails at validation/collection instead of only at runtime import.
USER_FUNCTION_NAME_PATTERN = re.compile(r"^(?P<module>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*):(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$")


class ConfigOptions(StrEnum):
    """Configuration option names for the pytest-httpchain plugin.

    These options can be set in pytest.ini or pyproject.toml under [tool.pytest.ini_options].
    All names carry the ``httpchain_`` prefix: pytest ini options live in one
    global namespace shared by every installed plugin, so generic names like
    ``suffix`` risk hard startup collisions. The pre-0.10 un-prefixed spellings
    were deprecated through the 0.10 series and removed in 0.11.

    Attributes:
        SUFFIX: File suffix for HTTP test files (default: "http").
            Test files must match pattern: test_<name>.<suffix>.json
        REF_PARENT_TRAVERSAL_DEPTH: Maximum parent directory traversals allowed
            in $ref paths for security (default: "3").
        MAX_COMPREHENSION_LENGTH: Maximum length for list/dict comprehensions
            in template expressions (default: "50000").
        MAX_PARALLEL_ITERATIONS: Maximum number of parallel iterations allowed
            per stage (default: "10000").

    Note: the HAR output directory is a CLI flag (``--httpchain-output-dir``),
    not an ini option, so it is intentionally not listed here.
    """

    SUFFIX = "httpchain_suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "httpchain_ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "httpchain_max_comprehension_length"
    MAX_PARALLEL_ITERATIONS = "httpchain_max_parallel_iterations"
