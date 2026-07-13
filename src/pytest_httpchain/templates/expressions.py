import re

# Pattern that handles nested braces in expressions
# Uses negative lookahead (?:(?!\}\}).)+ to match any character
# that is not followed by }}, allowing single } in dict literals
# Note: If a dict literal ends with }}, add a space before the closing }}
# Example: {{ {'key': value} }} instead of {{ {'key': value}}}
#
# Single-line only: `.` does not match newlines and the pattern is compiled
# without re.DOTALL, so an expression that spans newlines (e.g. a multi-line
# comprehension) is NOT recognised as a template. This is intentional —
# template values come from individual JSON string scalars, which are
# single-line in practice, and every consumer (substitution.py, models/types.py,
# the validator) shares this one pattern, so keeping it single-line keeps their
# behaviour aligned. Write multi-line logic in a user function instead.
TEMPLATE_PATTERN = r"\{\{(?P<expr>(?:(?!\}\}).)+)\}\}"


def is_complete_template(value: str) -> bool:
    """Check if a string is a complete template expression."""
    # Delegate to the single matcher so the "complete template" definition has one
    # source of truth (predicate and extractor cannot drift apart).
    return extract_template_expression(value) is not None


def extract_template_expression(value: str) -> str | None:
    """Extract the expression part from a complete template string."""
    # fullmatch() already anchors both ends, so no leading ^ / trailing $ is needed.
    if match := re.fullmatch(rf"\s*{TEMPLATE_PATTERN}\s*", value):
        return match.group("expr").strip()
    return None
