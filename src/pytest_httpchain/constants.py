from enum import StrEnum


class ConfigOptions(StrEnum):
    """Configuration option names for the pytest-httpchain plugin."""

    SUFFIX = "suffix"
    REF_PARENT_TRAVERSAL_DEPTH = "ref_parent_traversal_depth"
    MAX_COMPREHENSION_LENGTH = "max_comprehension_length"
