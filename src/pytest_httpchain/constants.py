"""Configuration constants for pytest-httpchain plugin.

This module defines configuration option names that can be set in pytest.ini
or pyproject.toml to customize plugin behavior.
"""

from enum import StrEnum


class ConfigOptions(StrEnum):
    """Configuration option names for the pytest-httpchain plugin.

    These options can be set in pytest.ini or pyproject.toml under [tool.pytest.ini_options].
    All names carry the ``httpchain_`` prefix: pytest ini options live in one
    global namespace shared by every installed plugin, so generic names like
    ``suffix`` risk hard startup collisions. The pre-0.10 un-prefixed spellings
    are still accepted as deprecated aliases (see ``LEGACY_INI_NAMES``) and will
    be removed in 0.11.

    Attributes:
        SUFFIX: File suffix for HTTP test files (default: "http").
            Test files must match pattern: test_<name>.<suffix>.json
        REF_PARENT_TRAVERSAL_DEPTH: Maximum parent directory traversals allowed
            in $ref paths for security (default: "3").
        MAX_COMPREHENSION_LENGTH: Maximum length for list/dict comprehensions
            in template expressions (default: "50000").
        MAX_PARALLEL_ITERATIONS: Maximum number of parallel iterations allowed
            per stage (default: "10000").

    Note: the HAR output directory is a CLI flag (``--httpchain-output-dir``,
    with ``--output-dir`` as a deprecated alias), not an ini option, so it is
    intentionally not listed here.
    """

    SUFFIX = "httpchain_suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "httpchain_ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "httpchain_max_comprehension_length"
    MAX_PARALLEL_ITERATIONS = "httpchain_max_parallel_iterations"


# Deprecated pre-0.10 spellings, accepted through the 0.10 series and removed
# in 0.11. The read helper in plugin.py prefers the new name when both are set.
LEGACY_INI_NAMES: dict[ConfigOptions, str] = {option: option.removeprefix("httpchain_") for option in ConfigOptions}
