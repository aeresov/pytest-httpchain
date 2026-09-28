"""Unit tests for custom type validators in types.py.

Each validated type is exercised through a ``TypeAdapter``: accepted values
pass through unchanged, rejected ones raise with the validator's own message.
"""

import json
from types import SimpleNamespace

import jsonschema
import pytest
from pydantic import TypeAdapter, ValidationError

from pytest_httpchain.models.types import (
    Base64String,
    FunctionImportName,
    GraphQLQuery,
    JMESPathExpression,
    JSONSchemaInline,
    PartialTemplateStr,
    RegexPattern,
    TemplateExpression,
    VariableName,
    XMLString,
    check_json_schema,
    convert_namespace_to_dict,
    json_schema_validator_class,
)
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, nested


def validate(annotated_type, value):
    return TypeAdapter(annotated_type).validate_python(value)


class TestVariableName:
    @pytest.mark.parametrize("value", ["foo", "bar_baz", "_private", "CamelCase", "var1", "item_2"])
    def test_valid(self, value):
        assert validate(VariableName, value) == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("1invalid", "Invalid Python variable name"),
            ("foo-bar", "Invalid Python variable name"),
            ("foo.bar", "Invalid Python variable name"),
            ("with space", "Invalid Python variable name"),
            ("class", "Python keyword is used"),
            ("def", "Python keyword is used"),
            ("return", "Python keyword is used"),
            # Soft keywords too.
            ("match", "Python keyword is used"),
            ("case", "Python keyword is used"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(VariableName, value)


class TestFunctionImportName:
    @pytest.mark.parametrize("value", ["module:func", "package.module:func", "a.b.c.d:my_func"])
    def test_valid(self, value):
        assert validate(FunctionImportName, value) == value

    def test_bare_name_rejected_with_hint(self):
        """A module-less name fails validation with the actionable format hint
        (previously it validated and only failed at runtime import)."""
        with pytest.raises(ValidationError, match="Module path is required"):
            validate(FunctionImportName, "my_function")

    @pytest.mark.parametrize("value", ["123invalid", "module:123func", "module::func"])
    def test_invalid_format(self, value):
        with pytest.raises(ValidationError, match="Invalid function name format"):
            validate(FunctionImportName, value)


class TestJMESPathExpression:
    @pytest.mark.parametrize(
        "value",
        ["data", "data.value", "items[0]", "data.items[*].name", "response.body.users[?age > `18`]", "items | [0]"],
    )
    def test_valid(self, value):
        assert validate(JMESPathExpression, value) == value

    @pytest.mark.parametrize("value", ["[invalid", "data..value"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            validate(JMESPathExpression, value)


class TestRegexPattern:
    @pytest.mark.parametrize("value", [r"\d+", r"[a-z]+", "hello", r"^\d{3}-\d{4}$", r"(?:https?://)?[\w.-]+"])
    def test_valid(self, value):
        assert validate(RegexPattern, value) == value

    @pytest.mark.parametrize("value", ["[invalid", "(unclosed"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid regular expression"):
            validate(RegexPattern, value)


class TestXMLString:
    @pytest.mark.parametrize(
        "value",
        [
            "<root>content</root>",
            '<user id="1" active="true">John</user>',
            "<root><child><grandchild>value</grandchild></child></root>",
        ],
    )
    def test_valid(self, value):
        assert validate(XMLString, value) == value

    def test_valid_with_namespaces_from_file(self, datadir):
        xml = (datadir / "xml_with_namespace.xml").read_text()
        assert validate(XMLString, xml) == xml

    @pytest.mark.parametrize("value", ["<root>content", "<root>content</other>", "not xml at all"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid XML"):
            validate(XMLString, value)


class TestGraphQLQuery:
    @pytest.mark.parametrize(
        "value",
        [
            "{ user { id name } }",
            "query GetUser { user { id name email } }",
            "mutation CreateUser($name: String!) { createUser(name: $name) { id } }",
            "query GetUsers($limit: Int) { users(limit: $limit) { id name } }",
        ],
    )
    def test_valid(self, value):
        assert validate(GraphQLQuery, value) == value

    def test_valid_with_fragments_from_file(self, datadir):
        query = (datadir / "graphql_with_fragments.graphql").read_text()
        assert validate(GraphQLQuery, query) == query

    @pytest.mark.parametrize("value", ["{ user { id name }", "not a graphql query"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)


class TestTemplateExpression:
    """A complete ``{{ expr }}`` — nothing outside the braces."""

    @pytest.mark.parametrize("value", ["{{ value }}", "{{ foo }}", "{{ a + b }}", "{{ user.name }}", "{{ name | upper }}"])
    def test_valid(self, value):
        assert validate(TemplateExpression, value) == value

    @pytest.mark.parametrize("value", ["prefix {{ value }}", "{{ value }} suffix", "just a string"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Must be a complete template expression"):
            validate(TemplateExpression, value)


class TestPartialTemplateStr:
    """At least one non-empty ``{{ expr }}`` anywhere in the string."""

    @pytest.mark.parametrize("value", ["{{ value }}", "Hello {{ name }}!", "prefix {{ value }} suffix", "{{ first }} and {{ second }}"])
    def test_valid(self, value):
        assert validate(PartialTemplateStr, value) == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("no template here", "Must contain at least one template expression"),
            ("{{  }}", "Template expression cannot be empty"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(PartialTemplateStr, value)


class TestBase64String:
    @pytest.mark.parametrize("value", ["SGVsbG8=", "dGVzdA==", ""])
    def test_valid(self, value):
        assert validate(Base64String, value) == value

    @pytest.mark.parametrize("value", ["not-valid-base64!!!", pytest.param("SGVsbG8", id="missing-padding")])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid base64 encoding"):
            validate(Base64String, value)


class TestJSONSchemaInline:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param({"type": "string"}, id="simple"),
            pytest.param(
                {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "integer"}}, "required": ["name"]},
                id="object",
            ),
            pytest.param(
                {"$schema": "http://json-schema.org/draft-07/schema#", "type": "object", "properties": {"id": {"type": "integer"}}},
                id="declared-dialect",
            ),
        ],
    )
    def test_valid(self, value):
        assert validate(JSONSchemaInline, value) == value

    def test_valid_complex_schema_from_file(self, datadir):
        schema = json.loads((datadir / "object_schema.json").read_text())
        assert validate(JSONSchemaInline, schema) == schema

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            pytest.param({"type": "not_a_type"}, "Invalid JSON Schema", id="unknown-type"),
            pytest.param({"properties": "not_an_object"}, "Invalid JSON Schema", id="bad-structure"),
            # Not a SchemaError: jsonschema itself fails on the non-string dialect.
            pytest.param({"$schema": 123}, "JSON Schema validation error", id="non-string-dialect"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(JSONSchemaInline, value)


class TestCheckJsonSchema:
    @pytest.mark.parametrize(
        "schema",
        [
            pytest.param({"$schema": "http://json-schema.org/draft-07/schema#", "type": "object"}, id="draft-07"),
            pytest.param({"$schema": "http://json-schema.org/draft-04/schema#", "type": "string"}, id="draft-04"),
            pytest.param({"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "array", "items": {"type": "string"}}, id="2020-12"),
            pytest.param({"type": "object", "properties": {"id": {"type": "integer"}}}, id="no-dialect"),
        ],
    )
    def test_valid_schema_passes(self, schema):
        check_json_schema(schema)

    def test_invalid_schema_raises(self):
        with pytest.raises(jsonschema.SchemaError):
            check_json_schema({"type": "invalid_type"})

    def test_schema_without_dialect_uses_draft_2020_12(self):
        """The fallback is the dialect ``jsonschema.validate`` picks, so the
        meta-check and instance validation agree."""
        assert json_schema_validator_class({"type": "object"}) is jsonschema.Draft202012Validator


class TestConvertNamespaceToDict:
    """The validator behind JSON bodies, inline schemas and jmespath operands."""

    def test_converts_at_every_depth_keeping_key_order(self):
        value = {"b": 1, "a": SimpleNamespace(z=[1, SimpleNamespace(y=2)], x={}), "c": [{"d": SimpleNamespace()}]}
        converted = convert_namespace_to_dict(value)
        assert converted == {"b": 1, "a": {"z": [1, {"y": 2}], "x": {}}, "c": [{"d": {}}]}
        assert list(converted) == ["b", "a", "c"]
        assert list(converted["a"]) == ["z", "x"]

    def test_value_nested_past_the_recursion_limit(self):
        """Iterative: the loader accepts any depth, and a recursive walk overflowed."""
        converted = convert_namespace_to_dict(nested(SimpleNamespace(leaf=1), BEYOND_RECURSION_LIMIT))
        # Unwrapped level by level: == on it recurses in C, which Windows caps
        # at 3,000 levels.
        for level in reversed(range(BEYOND_RECURSION_LIMIT)):
            converted = converted[0] if level % 2 else converted["k"]
        assert converted == {"leaf": 1}

    def test_shared_value_is_copied_where_it_occurs(self):
        shared = [SimpleNamespace(k=1)]
        converted = convert_namespace_to_dict({"a": shared, "b": shared})
        assert converted == {"a": [{"k": 1}], "b": [{"k": 1}]}
        assert converted["a"] is not converted["b"]

    def test_value_that_contains_itself_is_refused(self):
        """A plain loop would never end on it (the recursive walk overflowed)."""
        cyclic: list = []
        cyclic.append(cyclic)
        with pytest.raises(ValueError, match="contains itself"):
            convert_namespace_to_dict(cyclic)
