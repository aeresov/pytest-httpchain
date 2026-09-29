"""Ini option names, the scenario file-name grammar and the user-function name grammar."""

import functools
import re
from enum import StrEnum

# "module.path:function_name": a required dotted module path (no leading,
# trailing or doubled dots) and a single identifier. Shared by the models'
# validator and the importer, so a bare name fails at validation, not at import.
# Anchored with \A and \Z, not ^ and $: "$" also matches just before a trailing
# "\n", so match() against "^...$" accepted "mod:func\n". The pattern is exported
# as userfunc.NAME_PATTERN, so match() has to be as strict as fullmatch() here.
USER_FUNCTION_NAME_PATTERN = re.compile(r"\A(?P<module>[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*):(?P<function>[a-zA-Z_][a-zA-Z0-9_]*)\Z")

_BARE_NAME_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")


def parse_user_function_name(name: str) -> tuple[str, str]:
    """Split ``module.path:function_name`` into ``(module_path, function_name)``,
    raising ``ValueError`` that says what is wrong with an unusable name.

    The wording is shared, not just the grammar: the models' validator surfaces
    it as a validation error and the importer as a ``UserFunctionError``, and one
    mistake must not be described to the author in two different ways.
    """
    if match := USER_FUNCTION_NAME_PATTERN.fullmatch(name):
        return match["module"], match["function"]
    # The most common mistake deserves an actionable hint.
    if _BARE_NAME_PATTERN.fullmatch(name):
        raise ValueError(f"Module path is required: use 'module:{name}' format instead of '{name}'")
    raise ValueError(f"Invalid function name format: {name}")


# What a scenario file's name ends with, after ``test_<name>.<suffix>``: pytest
# collects both, and ``validate`` takes both without a warning. The reader is the
# same for both (``jsonref.loads_jsonc``, comments and trailing commas allowed in
# any file), so ``.jsonc`` only tells editors to expect JSON with comments.
SCENARIO_FILE_EXTENSIONS = (".json", ".jsonc")

# The ``httpchain_suffix`` when the ini option is unset, and what a value must
# look like: it sits between two dots of a file name.
DEFAULT_SUFFIX = "http"
_SUFFIX_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,32}")


def check_suffix(suffix: str) -> str:
    """Return ``suffix``, or raise ``ValueError`` saying what is wrong with it.

    One rule in one wording, for the ini option (``pytest_configure``, and
    ``validate`` reading it from the file pytest would) and for ``validate
    --suffix``; each caller names the value it checked.
    """
    if not _SUFFIX_PATTERN.fullmatch(suffix):
        raise ValueError("must contain only alphanumeric characters, underscores, hyphens, and be ≤32 chars")
    return suffix


@functools.cache
def scenario_file_pattern(suffix: str) -> re.Pattern[str]:
    """The names pytest collects as scenarios: ``test_<name>.<suffix>`` and one
    of the extensions, matched whole (``fullmatch``), with ``<name>`` as the
    ``name`` group. ``pytest_collect_file`` and ``validate``'s directory search
    both match by it, so ``validate tests/`` checks what ``pytest tests/`` runs.
    """
    extensions = "|".join(re.escape(extension) for extension in SCENARIO_FILE_EXTENSIONS)
    return re.compile(rf"test_(?P<name>.+)\.{re.escape(suffix)}(?:{extensions})")


class ConfigOptions(StrEnum):
    """Ini option names, settable in pytest.ini or [tool.pytest.ini_options].

    The ``httpchain_`` prefix is required: pytest ini options share one global
    namespace across plugins. Defaults are registered in
    ``plugin.pytest_addoption`` (the redaction lists come from ``redaction``);
    the HAR output directory is a CLI flag, not an ini option.
    """

    SUFFIX = "httpchain_suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "httpchain_ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "httpchain_max_comprehension_length"
    MAX_PARALLEL_ITERATIONS = "httpchain_max_parallel_iterations"
    REDACT_HEADERS = "httpchain_redact_headers"
    REDACT_QUERY_PARAMS = "httpchain_redact_query_params"
    HAR_REDACT = "httpchain_har_redact"
