"""The ``{{ expression }}`` engine: simpleeval-backed evaluation of scenario
values, with variables, functions and comprehensions.

    >>> walk({"url": "https://api.example.com/users/{{ user_id }}"}, {"user_id": 123})
    {'url': 'https://api.example.com/users/123'}
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
