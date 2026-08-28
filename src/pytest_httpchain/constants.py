"""Ini option names and the user-function name grammar."""

import re
from enum import StrEnum

# "module.path:function_name": a required dotted module path (no leading,
# trailing or doubled dots) and a single identifier. Shared by the models'
# validator and the importer, so a bare name fails at validation, not at import.
USER_FUNCTION_NAME_PATTERN = re.compile(r"^(?P<module>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*):(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)$")

_BARE_NAME_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def user_function_name_problem(name: str) -> str | None:
    """Why ``name`` is not a usable ``module.path:function_name``, or None.

    The wording is shared, not just the grammar: the models' validator raises it
    as a ``ValueError`` and the importer as a ``UserFunctionError``, and one
    mistake must not be described to the author in two different ways.
    """
    if USER_FUNCTION_NAME_PATTERN.match(name):
        return None
    # The most common mistake deserves an actionable hint.
    if _BARE_NAME_PATTERN.fullmatch(name):
        return f"Module path is required: use 'module:{name}' format instead of '{name}'"
    return f"Invalid function name format: {name}"


class ConfigOptions(StrEnum):
    """Ini option names, settable in pytest.ini or [tool.pytest.ini_options].

    The ``httpchain_`` prefix is required: pytest ini options share one global
    namespace across plugins. Defaults live in ``plugin.pytest_addoption``; the
    HAR output directory is a CLI flag, not an ini option.
    """

    SUFFIX = "httpchain_suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "httpchain_ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "httpchain_max_comprehension_length"
    MAX_PARALLEL_ITERATIONS = "httpchain_max_parallel_iterations"
