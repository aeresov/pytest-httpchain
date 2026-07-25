import re

# The lookahead allows a single `}` inside the expression (dict literals), so
# `{{ {'k': v} }}` needs the space before the closing braces. Single-line by
# design: template values are JSON string scalars, and every consumer shares
# this pattern. Multi-line logic belongs in a user function.
_TEMPLATE_INNER = r"(?:(?!\}\}).)+"
TEMPLATE_PATTERN = r"\{\{(?P<expr>" + _TEMPLATE_INNER + r")\}\}"
# For JSON Schema `pattern` sites: same semantics, no named group, which JS
# regex engines reject (and VS Code then silently drops the pattern).
TEMPLATE_PATTERN_ECMA = r"\{\{" + _TEMPLATE_INNER + r"\}\}"


def is_complete_template(value: str) -> bool:
    """True when the whole string is one ``{{ }}`` expression."""
    return extract_template_expression(value) is not None


def extract_template_expression(value: str) -> str | None:
    """The expression inside a complete template string, else None."""
    if match := re.fullmatch(rf"\s*{TEMPLATE_PATTERN}\s*", value):
        return match.group("expr").strip()
    return None
