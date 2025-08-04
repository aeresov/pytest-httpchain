"""Shared template pattern for validation and substitution."""

import re

# Pattern for matching template expressions with named groups
# This pattern is used both for validation and substitution
TEMPLATE_PATTERN = r"(?P<open>\{\{)(?P<expr>[^}]+?)(?P<close>\}\})"
TEMPLATE_REGEX = re.compile(TEMPLATE_PATTERN)

# Pattern for matching a complete template expression (for validation)
# This ensures the entire string is a template, not just contains one
COMPLETE_TEMPLATE_PATTERN = rf"^\s*{TEMPLATE_PATTERN}\s*$"
COMPLETE_TEMPLATE_REGEX = re.compile(COMPLETE_TEMPLATE_PATTERN)


def is_complete_template(value: str) -> bool:
    """Check if a string is a complete template expression."""
    return COMPLETE_TEMPLATE_REGEX.match(value) is not None


def extract_template_expression(value: str) -> str | None:
    """Extract the expression part from a complete template string."""
    match = COMPLETE_TEMPLATE_REGEX.match(value)
    if match:
        return match.group("expr").strip()
    return None
