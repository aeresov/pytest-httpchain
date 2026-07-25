"""Ini option names and the user-function name grammar."""

import re
from enum import StrEnum

# "module.path:function_name": a required dotted module path (no leading,
# trailing or doubled dots) and a single identifier. Shared by the models'
# validator and the importer, so a bare name fails at validation, not at import.
USER_FUNCTION_NAME_PATTERN = re.compile(r"^(?P<module>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*):(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$")


class ConfigOptions(StrEnum):
    """Ini option names, settable in pytest.ini or [tool.pytest.ini_options].

    The ``httpchain_`` prefix is required: pytest ini options share one global
    namespace across plugins. Defaults live in ``plugin._INI_DEFAULTS``; the HAR
    output directory is a CLI flag, not an ini option.
    """

    SUFFIX = "httpchain_suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "httpchain_ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "httpchain_max_comprehension_length"
    MAX_PARALLEL_ITERATIONS = "httpchain_max_parallel_iterations"
