"""Template expression engine for pytest-httpchain.

This package provides the {{ expression }} substitution engine used to
process dynamic values in test scenarios. Expressions are evaluated safely
using simpleeval with support for variables, functions, and comprehensions.

Example:
    >>> from pytest_httpchain.templates import walk
    >>> data = {"url": "https://api.example.com/users/{{ user_id }}"}
    >>> result = walk(data, {"user_id": 123})
    >>> result["url"]
    'https://api.example.com/users/123'
"""

from pytest_httpchain.templates.exceptions import TemplatesError
from pytest_httpchain.templates.expressions import TEMPLATE_PATTERN, TEMPLATE_PATTERN_ECMA, extract_template_expression, is_complete_template
from pytest_httpchain.templates.substitution import TEMPLATE_BUILTINS, contains_template, set_max_comprehension_length, walk

__all__ = [
    "walk",
    "contains_template",
    "set_max_comprehension_length",
    "is_complete_template",
    "extract_template_expression",
    "TEMPLATE_PATTERN",
    "TEMPLATE_PATTERN_ECMA",
    "TEMPLATE_BUILTINS",
    "TemplatesError",
]
