"""Unit tests for custom type validators in types.py."""

import json
import warnings

import pytest
from pydantic import BaseModel, ValidationError
from pytest_httpchain_models.types import (
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
)

# Suppress Pydantic warning about field name "schema" shadowing BaseModel attribute
warnings.filterwarnings("ignore", message=r'Field name "schema" in "TestJSONSchemaInline.Model" shadows an attribute', category=UserWarning)


class TestVariableName:
    """Tests for VariableName validator (Python identifier)."""

    class Model(BaseModel):
        name: VariableName

    def test_valid_simple_name(self):
        """Test valid simple variable names."""
        assert self.Model(name="foo").name == "foo"
        assert self.Model(name="bar_baz").name == "bar_baz"
        assert self.Model(name="_private").name == "_private"
        assert self.Model(name="CamelCase").name == "CamelCase"

    def test_valid_with_numbers(self):
        """Test valid names with numbers."""
        assert self.Model(name="var1").name == "var1"
        assert self.Model(name="item_2").name == "item_2"

    def test_invalid_starts_with_number(self):
        """Test that names starting with numbers are rejected."""
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            self.Model(name="1invalid")

    def test_invalid_contains_special_chars(self):
        """Test that names with special characters are rejected."""
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            self.Model(name="foo-bar")
        with pytest.raises(ValidationError, match="Invalid Python variable name"):
            self.Model(name="foo.bar")

    def test_invalid_python_keyword(self):
        """Test that Python keywords are rejected."""
        with pytest.raises(ValidationError, match="Python keyword is used"):
            self.Model(name="class")
        with pytest.raises(ValidationError, match="Python keyword is used"):
            self.Model(name="def")
        with pytest.raises(ValidationError, match="Python keyword is used"):
            self.Model(name="return")

    def test_invalid_soft_keyword(self):
        """Test that soft keywords are rejected."""
        with pytest.raises(ValidationError, match="Python keyword is used"):
            self.Model(name="match")
        with pytest.raises(ValidationError, match="Python keyword is used"):
            self.Model(name="case")


class TestFunctionImportName:
    """Tests for FunctionImportName validator."""

    class Model(BaseModel):
        func: FunctionImportName

    def test_valid_simple_function(self):
        """Test simple function name without module."""
        assert self.Model(func="my_function").func == "my_function"

    def test_valid_with_module(self):
        """Test function with module path."""
        assert self.Model(func="module:func").func == "module:func"
        assert self.Model(func="package.module:func").func == "package.module:func"
        assert self.Model(func="a.b.c.d:my_func").func == "a.b.c.d:my_func"

    def test_invalid_format(self):
        """Test invalid function name formats."""
        with pytest.raises(ValidationError, match="Invalid function name format"):
            self.Model(func="123invalid")
        with pytest.raises(ValidationError, match="Invalid function name format"):
            self.Model(func="module:123func")
        with pytest.raises(ValidationError, match="Invalid function name format"):
            self.Model(func="module::func")


class TestJMESPathExpression:
    """Tests for JMESPathExpression validator."""

    class Model(BaseModel):
        expr: JMESPathExpression

    def test_valid_simple_path(self):
        """Test simple JMESPath expressions."""
        assert self.Model(expr="data").expr == "data"
        assert self.Model(expr="data.value").expr == "data.value"
        assert self.Model(expr="items[0]").expr == "items[0]"

    def test_valid_complex_path(self):
        """Test complex JMESPath expressions."""
        assert self.Model(expr="data.items[*].name").expr == "data.items[*].name"
        assert self.Model(expr="response.body.users[?age > `18`]").expr == "response.body.users[?age > `18`]"
        assert self.Model(expr="items | [0]").expr == "items | [0]"

    def test_invalid_jmespath(self):
        """Test invalid JMESPath expressions."""
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            self.Model(expr="[invalid")
        with pytest.raises(ValidationError, match="Invalid JMESPath expression"):
            self.Model(expr="data..value")


class TestRegexPattern:
    """Tests for RegexPattern validator."""

    class Model(BaseModel):
        pattern: RegexPattern

    def test_valid_simple_patterns(self):
        """Test simple regex patterns."""
        assert self.Model(pattern=r"\d+").pattern == r"\d+"
        assert self.Model(pattern=r"[a-z]+").pattern == r"[a-z]+"
        assert self.Model(pattern="hello").pattern == "hello"

    def test_valid_complex_patterns(self):
        """Test complex regex patterns."""
        assert self.Model(pattern=r"^\d{3}-\d{4}$").pattern == r"^\d{3}-\d{4}$"
        assert self.Model(pattern=r"(?:https?://)?[\w.-]+").pattern == r"(?:https?://)?[\w.-]+"

    def test_invalid_regex(self):
        """Test invalid regex patterns."""
        with pytest.raises(ValidationError, match="Invalid regular expression"):
            self.Model(pattern="[invalid")
        with pytest.raises(ValidationError, match="Invalid regular expression"):
            self.Model(pattern="(unclosed")


class TestXMLString:
    """Tests for XMLString validator."""

    class Model(BaseModel):
        xml: XMLString

    def test_valid_simple_xml(self):
        """Test simple valid XML."""
        xml = "<root>content</root>"
        assert self.Model(xml=xml).xml == xml

    def test_valid_with_attributes(self):
        """Test XML with attributes."""
        xml = '<user id="1" active="true">John</user>'
        assert self.Model(xml=xml).xml == xml

    def test_valid_nested_xml(self):
        """Test nested XML structure."""
        xml = "<root><child><grandchild>value</grandchild></child></root>"
        assert self.Model(xml=xml).xml == xml

    def test_valid_with_namespace(self, datadir):
        """Test XML with namespace loaded from file."""
        xml = (datadir / "xml_with_namespace.xml").read_text()
        assert self.Model(xml=xml).xml == xml
        assert "soap:Envelope" in xml
        assert "xmlns:soap" in xml

    def test_invalid_xml_unclosed_tag(self):
        """Test invalid XML with unclosed tag."""
        with pytest.raises(ValidationError, match="Invalid XML"):
            self.Model(xml="<root>content")

    def test_invalid_xml_mismatched_tags(self):
        """Test invalid XML with mismatched tags."""
        with pytest.raises(ValidationError, match="Invalid XML"):
            self.Model(xml="<root>content</other>")

    def test_invalid_xml_malformed(self):
        """Test malformed XML."""
        with pytest.raises(ValidationError, match="Invalid XML"):
            self.Model(xml="not xml at all")


class TestGraphQLQuery:
    """Tests for GraphQLQuery validator."""

    class Model(BaseModel):
        query: GraphQLQuery

    def test_valid_simple_query(self):
        """Test simple GraphQL query."""
        query = "{ user { id name } }"
        assert self.Model(query=query).query == query

    def test_valid_query_with_operation(self):
        """Test query with operation name."""
        query = "query GetUser { user { id name email } }"
        assert self.Model(query=query).query == query

    def test_valid_mutation(self):
        """Test GraphQL mutation."""
        query = "mutation CreateUser($name: String!) { createUser(name: $name) { id } }"
        assert self.Model(query=query).query == query

    def test_valid_with_variables(self):
        """Test query with variable definitions."""
        query = "query GetUsers($limit: Int) { users(limit: $limit) { id name } }"
        assert self.Model(query=query).query == query

    def test_valid_with_fragments(self, datadir):
        """Test query with fragments loaded from file."""
        query = (datadir / "graphql_with_fragments.graphql").read_text()
        assert self.Model(query=query).query == query
        assert "fragment userFields" in query

    def test_invalid_graphql_syntax(self):
        """Test invalid GraphQL syntax."""
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            self.Model(query="{ user { id name }")  # Missing closing brace

    def test_invalid_graphql_malformed(self):
        """Test malformed GraphQL."""
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            self.Model(query="not a graphql query")


class TestTemplateExpression:
    """Tests for TemplateExpression validator (complete {{ expr }})."""

    class Model(BaseModel):
        expr: TemplateExpression

    def test_valid_simple_expression(self):
        """Test simple template expression."""
        assert self.Model(expr="{{ value }}").expr == "{{ value }}"
        assert self.Model(expr="{{ foo }}").expr == "{{ foo }}"

    def test_valid_with_operations(self):
        """Test template with operations."""
        assert self.Model(expr="{{ a + b }}").expr == "{{ a + b }}"
        assert self.Model(expr="{{ user.name }}").expr == "{{ user.name }}"

    def test_valid_with_filters(self):
        """Test template with filters."""
        assert self.Model(expr="{{ name | upper }}").expr == "{{ name | upper }}"

    def test_invalid_not_complete_template(self):
        """Test that partial templates are rejected."""
        with pytest.raises(ValidationError, match="Must be a complete template expression"):
            self.Model(expr="prefix {{ value }}")
        with pytest.raises(ValidationError, match="Must be a complete template expression"):
            self.Model(expr="{{ value }} suffix")

    def test_invalid_plain_string(self):
        """Test that plain strings are rejected."""
        with pytest.raises(ValidationError, match="Must be a complete template expression"):
            self.Model(expr="just a string")


class TestPartialTemplateStr:
    """Tests for PartialTemplateStr validator (contains at least one {{ expr }})."""

    class Model(BaseModel):
        text: PartialTemplateStr

    def test_valid_complete_template(self):
        """Test that complete templates are also valid."""
        assert self.Model(text="{{ value }}").text == "{{ value }}"

    def test_valid_partial_template(self):
        """Test partial template with prefix/suffix."""
        assert self.Model(text="Hello {{ name }}!").text == "Hello {{ name }}!"
        assert self.Model(text="prefix {{ value }} suffix").text == "prefix {{ value }} suffix"

    def test_valid_multiple_expressions(self):
        """Test string with multiple template expressions."""
        text = "{{ first }} and {{ second }}"
        assert self.Model(text=text).text == text

    def test_invalid_no_template(self):
        """Test that strings without templates are rejected."""
        with pytest.raises(ValidationError, match="Must contain at least one template expression"):
            self.Model(text="no template here")

    def test_invalid_empty_expression(self):
        """Test that empty template expressions are rejected."""
        with pytest.raises(ValidationError, match="Template expression cannot be empty"):
            self.Model(text="{{  }}")


class TestBase64String:
    """Tests for Base64String validator."""

    class Model(BaseModel):
        data: Base64String

    def test_valid_base64(self):
        """Test valid base64 strings."""
        assert self.Model(data="SGVsbG8=").data == "SGVsbG8="  # "Hello"
        assert self.Model(data="dGVzdA==").data == "dGVzdA=="  # "test"

    def test_valid_empty_base64(self):
        """Test empty string (valid base64)."""
        assert self.Model(data="").data == ""

    def test_invalid_base64(self):
        """Test invalid base64 strings."""
        with pytest.raises(ValidationError, match="Invalid base64 encoding"):
            self.Model(data="not-valid-base64!!!")

    def test_invalid_padding(self):
        """Test base64 with incorrect padding."""
        with pytest.raises(ValidationError, match="Invalid base64 encoding"):
            self.Model(data="SGVsbG8")  # Missing '='


class TestJSONSchemaInline:
    """Tests for JSONSchemaInline validator."""

    class Model(BaseModel):
        schema: JSONSchemaInline

    def test_valid_simple_schema(self):
        """Test simple valid JSON schema."""
        schema = {"type": "string"}
        assert self.Model(schema=schema).schema == schema

    def test_valid_object_schema(self):
        """Test object schema with properties."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "age": {"type": "integer"},
            },
            "required": ["name"],
        }
        assert self.Model(schema=schema).schema == schema

    def test_valid_complex_schema_from_file(self, datadir):
        """Test complex object schema loaded from file."""
        schema = json.loads((datadir / "object_schema.json").read_text())
        result = self.Model(schema=schema)
        assert result.schema["type"] == "object"
        assert "id" in result.schema["properties"]
        assert "roles" in result.schema["properties"]
        assert result.schema["required"] == ["id", "name", "email"]

    def test_valid_with_schema_version(self):
        """Test schema with $schema version."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"id": {"type": "integer"}},
        }
        assert self.Model(schema=schema).schema == schema

    def test_invalid_schema_unknown_type(self):
        """Test schema with unknown type."""
        with pytest.raises(ValidationError, match="Invalid JSON Schema"):
            self.Model(schema={"type": "not_a_type"})

    def test_invalid_schema_bad_structure(self):
        """Test schema with invalid structure."""
        with pytest.raises(ValidationError, match="Invalid JSON Schema"):
            self.Model(schema={"properties": "not_an_object"})


class TestCheckJsonSchema:
    """Tests for the check_json_schema utility function."""

    def test_valid_draft7_schema(self):
        """Test valid Draft-07 schema."""
        schema = {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
        }
        check_json_schema(schema)  # Should not raise

    def test_valid_draft4_schema(self):
        """Test valid Draft-04 schema."""
        schema = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "type": "string",
        }
        check_json_schema(schema)  # Should not raise

    def test_valid_2020_12_schema(self):
        """Test valid 2020-12 schema."""
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "array",
            "items": {"type": "string"},
        }
        check_json_schema(schema)  # Should not raise

    def test_default_to_draft7(self):
        """Test that schemas without $schema default to Draft-07."""
        schema = {"type": "object", "properties": {"id": {"type": "integer"}}}
        check_json_schema(schema)  # Should not raise

    def test_invalid_schema_raises(self):
        """Test that invalid schemas raise SchemaError."""
        import jsonschema

        with pytest.raises(jsonschema.SchemaError):
            check_json_schema({"type": "invalid_type"})
