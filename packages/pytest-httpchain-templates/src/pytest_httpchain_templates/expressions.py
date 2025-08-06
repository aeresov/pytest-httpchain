import re

TEMPLATE_PATTERN = r"(?P<open>\{\{)(?P<expr>[^}]+?)(?P<close>\}\})"
TEMPLATE_REGEX = re.compile(TEMPLATE_PATTERN)

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
