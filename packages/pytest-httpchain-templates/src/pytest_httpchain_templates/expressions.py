import re

TEMPLATE_PATTERN = r"\{\{(?P<expr>[^}]+?)\}\}"


def is_complete_template(value: str) -> bool:
    """Check if a string is a complete template expression."""
    return bool(re.fullmatch(rf"^\s*{TEMPLATE_PATTERN}\s*$", value))


def extract_template_expression(value: str) -> str | None:
    """Extract the expression part from a complete template string."""
    if match := re.fullmatch(rf"^\s*{TEMPLATE_PATTERN}\s*$", value):
        return match.group("expr").strip()
    return None
