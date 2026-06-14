"""Template expression engine for pytest-httpchain.

This package provides the {{ expression }} substitution engine used to
process dynamic values in test scenarios. Expressions are evaluated safely
using simpleeval with support for variables, functions, and comprehensions.

Example:
    >>> from pytest_httpchain_templates import walk
    >>> data = {"url": "https://api.example.com/users/{{ user_id }}"}
    >>> result = walk(data, {"user_id": 123})
    >>> result["url"]
    'https://api.example.com/users/123'
"""

from .exceptions import TemplatesError
from .expressions import TEMPLATE_PATTERN, extract_template_expression, is_complete_template
from .substitution import TEMPLATE_BUILTINS, walk

__all__ = [
    "walk",
    "is_complete_template",
    "extract_template_expression",
    "TEMPLATE_PATTERN",
    "TEMPLATE_BUILTINS",
    "TemplatesError",
]
