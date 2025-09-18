import re

# Pattern that handles nested braces in expressions
# Uses negative lookahead (?:(?!\}\}).)+ to match any character
# that is not followed by }}, allowing single } in dict literals
# Note: If a dict literal ends with }}, add a space before the closing }}
# Example: {{ {'key': value} }} instead of {{ {'key': value}}}
TEMPLATE_PATTERN = r"\{\{(?P<expr>(?:(?!\}\}).)+)\}\}"


def is_complete_template(value: str) -> bool:
    """Check if a string is a complete template expression."""
    return bool(re.fullmatch(rf"^\s*{TEMPLATE_PATTERN}\s*$", value))


def extract_template_expression(value: str) -> str | None:
    """Extract the expression part from a complete template string."""
    if match := re.fullmatch(rf"^\s*{TEMPLATE_PATTERN}\s*$", value):
        return match.group("expr").strip()
    return None
