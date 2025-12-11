"""Configuration constants for pytest-httpchain plugin.

This module defines configuration option names that can be set in pytest.ini
or pyproject.toml to customize plugin behavior.
"""

from enum import StrEnum


class ConfigOptions(StrEnum):
    """Configuration option names for the pytest-httpchain plugin.

    These options can be set in pytest.ini or pyproject.toml under [tool.pytest.ini_options].

    Attributes:
        SUFFIX: File suffix for HTTP test files (default: "http").
            Test files must match pattern: test_<name>.<suffix>.json
        REF_PARENT_TRAVERSAL_DEPTH: Maximum parent directory traversals allowed
            in $ref paths for security (default: "3").
        MAX_COMPREHENSION_LENGTH: Maximum length for list/dict comprehensions
            in template expressions (default: "50000").
    """

    SUFFIX = "suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "max_comprehension_length"
