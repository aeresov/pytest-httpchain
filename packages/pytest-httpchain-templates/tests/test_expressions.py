"""Tests for expressions.py - template pattern matching utilities."""

from pytest_httpchain_templates import extract_template_expression, is_complete_template


class TestIsCompleteTemplate:
    """Test the is_complete_template() function."""

    def test_complete_template(self):
        assert is_complete_template("{{ value }}") is True

    def test_complete_template_with_whitespace(self):
        assert is_complete_template("  {{ value }}  ") is True
        assert is_complete_template("\t{{ value }}\n") is True

    def test_complete_template_complex_expression(self):
        assert is_complete_template("{{ x + y * 2 }}") is True
        assert is_complete_template("{{ [i for i in items] }}") is True

    def test_incomplete_template_with_prefix(self):
        assert is_complete_template("Hello {{ name }}") is False

    def test_incomplete_template_with_suffix(self):
        assert is_complete_template("{{ name }} there") is False

    def test_multiple_templates(self):
        assert is_complete_template("{{ a }} {{ b }}") is False

    def test_no_template(self):
        assert is_complete_template("plain text") is False

    def test_empty_string(self):
        assert is_complete_template("") is False

    def test_partial_braces(self):
        assert is_complete_template("{{ incomplete") is False
        assert is_complete_template("incomplete }}") is False
        assert is_complete_template("{ value }") is False


class TestExtractTemplateExpression:
    """Test the extract_template_expression() function."""

    def test_extracts_simple_expression(self):
        assert extract_template_expression("{{ value }}") == "value"

    def test_extracts_with_internal_whitespace(self):
        assert extract_template_expression("{{  spaced  }}") == "spaced"

    def test_extracts_with_outer_whitespace(self):
        assert extract_template_expression("  {{ value }}  ") == "value"

    def test_extracts_complex_expression(self):
        assert extract_template_expression("{{ x + y * 2 }}") == "x + y * 2"

    def test_extracts_list_comprehension(self):
        result = extract_template_expression("{{ [i * 2 for i in items] }}")
        assert result == "[i * 2 for i in items]"

    def test_returns_none_for_non_template(self):
        assert extract_template_expression("not a template") is None

    def test_returns_none_for_mixed_content(self):
        assert extract_template_expression("Hello {{ name }}") is None

    def test_returns_none_for_multiple_templates(self):
        assert extract_template_expression("{{ a }} {{ b }}") is None

    def test_returns_none_for_empty_string(self):
        assert extract_template_expression("") is None

    def test_preserves_dict_literal_expression(self):
        # Dict literals with space before closing
        result = extract_template_expression("{{ {'key': value} }}")
        assert result == "{'key': value}"
