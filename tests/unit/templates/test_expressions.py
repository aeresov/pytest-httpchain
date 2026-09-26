"""Tests for expressions.py - template pattern matching utilities."""

import pytest

from pytest_httpchain.templates import extract_template_expression, is_complete_template


@pytest.mark.parametrize(
    ("value", "expr"),
    [
        ("{{ value }}", "value"),
        ("{{  spaced  }}", "spaced"),
        ("  {{ value }}  ", "value"),
        ("\t{{ value }}\n", "value"),
        ("{{ x + y * 2 }}", "x + y * 2"),
        ("{{ [i * 2 for i in items] }}", "[i * 2 for i in items]"),
        ("{{ {'key': value} }}", "{'key': value}"),
        ("Hello {{ name }}", None),
        ("{{ name }} there", None),
        ("{{ a }} {{ b }}", None),
        ("plain text", None),
        ("", None),
        ("{{ incomplete", None),
        ("incomplete }}", None),
        ("{ value }", None),
        # "{{ }}" carries nothing to evaluate and simpleeval raises on it at
        # runtime, so it is not a complete template — matching
        # validate_partial_template_str, which has always refused the empty form.
        ("{{}}", None),
        ("{{ }}", None),
        ("  {{   }}  ", None),
    ],
)
def test_complete_template_detection(value, expr):
    """Both predicates agree: a string is a complete template exactly when an
    expression can be extracted from it."""
    assert extract_template_expression(value) == expr
    assert is_complete_template(value) is (expr is not None)
