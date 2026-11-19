"""Unit tests for custom type validators in types.py.

Each validated type is exercised through a ``TypeAdapter``: accepted values
pass through unchanged, rejected ones raise with the validator's own message.
"""

import json
import pickle
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import jsonschema
import pytest
from pydantic import AfterValidator, TypeAdapter, ValidationError

from pytest_httpchain.models.types import (
    RENDERED,
    Base64String,
    FunctionImportName,
    GraphQLQuery,
    JMESPathExpression,
    JSONSchemaInline,
    PartialTemplateStr,
    RegexPattern,
    SchemaFileRef,
    SchemaFileRefStr,
    SerializablePath,
    TemplateExpression,
    VariableName,
    VarsNamespace,
    XMLString,
    check_json_schema,
    convert_dict_to_namespace,
    convert_namespace_items_to_dict,
    convert_namespace_to_dict,
    json_schema_validator_class,
    parse_schema_file_ref,
    validate_templated_graphql_query,
)
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, LOADABLE_BUT_DEEP, nested


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


def _nested_selection_sets(depth: int, innermost: str) -> str:
    """``innermost`` as the selection set of a field ``depth`` fields down."""
    return "{ a " * depth + innermost + " }" * depth


def _nested(kind: str, levels: int) -> str:
    """A query nested ``levels`` deep by ``kind``, counted as graphql-core
    3.2.14 and 3.3.2 count toward their limit: each selection set, list
    value, object value and list type is a level, whatever holds it."""
    match kind:
        case "selection-sets":
            return _nested_selection_sets(levels - 1, "{ id }")
        case "list-values":
            # Inside the argument of a field of the operation's selection set,
            # which is the first level.
            return "{ f(x: " + "[" * (levels - 1) + "1" + "]" * (levels - 1) + ") }"
        case "object-values":
            return "{ f(x: " + "{a: " * (levels - 1) + "1" + "}" * (levels - 1) + ") }"
        case "list-types":
            # A variable's type, outside every selection set.
            return "query ($v: " + "[" * levels + "Int" + "]" * levels + ") { f }"
        case "selection-sets-and-list-values":
            inner = levels // 2
            return _nested_selection_sets(levels - inner - 1, "{ f(x: " + "[" * inner + "1" + "]" * inner + ") }")
    raise AssertionError(kind)


NESTING_KINDS = ["selection-sets", "list-values", "object-values", "list-types", "selection-sets-and-list-values"]


class TestGraphQLQuery:
    """One verdict whichever graphql-core is installed: these run on the
    declared floor (3.2.10) and on the locked version, and the parsers differ.
    From 3.3.2 ``graphql.parse`` takes a selection set that selects nothing,
    which every version before it refused; 3.2.14 and 3.3.2 refuse a document
    nested more than 200 levels deep, which older versions parse until the
    interpreter's recursion limit; and 3.3 parses type-system syntax 3.2 does
    not, which a request's document never holds."""

    @pytest.mark.parametrize(
        "value",
        [
            "{ user { id name } }",
            "query GetUser { user { id name email } }",
            "mutation CreateUser($name: String!) { createUser(name: $name) { id } }",
            "query GetUsers($limit: Int) { users(limit: $limit) { id name } }",
            # Descriptions on an operation, a variable or a fragment, which
            # graphql-core parses from 3.2.10, the declared floor.
            pytest.param('"Who is asking." query Me { me { id } }', id="operation-description"),
            pytest.param('query User("The user to fetch." $id: ID!) { user(id: $id) { id } }', id="variable-description"),
            pytest.param('{ me { ...Fields } } """Shared fields.""" fragment Fields on User { id }', id="fragment-description"),
            # A query of several operations, the body naming one to execute.
            pytest.param("query Me { me { id } } query Users { users { id } }", id="several-named-operations"),
        ],
    )
    def test_valid(self, value):
        assert validate(GraphQLQuery, value) == value

    @pytest.mark.parametrize(
        "value",
        [
            # A server executes a request's document: a type-system definition
            # in it is refused, alone or beside an operation.
            pytest.param("type Query { me: User }", id="type"),
            pytest.param("schema { query: Query }", id="schema"),
            pytest.param("scalar DateTime", id="scalar"),
            pytest.param('extend type User @key(fields: "id")', id="type-extension"),
            pytest.param("{ me { id } } type User { id: ID }", id="beside-an-operation"),
            # Type-system syntax graphql-core 3.3 parses and 3.2 does not.
            pytest.param("directive @a @b on FIELD", id="directive-on-a-directive-definition"),
            pytest.param("directive @a on FRAGMENT_VARIABLE_DEFINITION", id="fragment-variable-location"),
            # Fragments alone hold no operation to execute, and a server refuses
            # them as never used.
            pytest.param("fragment Fields on User { id }", id="fragments-only"),
        ],
    )
    def test_non_executable_document_is_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)

    @pytest.mark.parametrize(
        "value",
        [
            # Refused by every server, whichever operation a request names:
            # one without a name cannot be told from another.
            pytest.param("{ me { id } } query Users { users { id } }", id="anonymous-beside-a-named-operation"),
            pytest.param("{ me { id } } { users { id } }", id="two-anonymous-operations"),
            pytest.param("query Me { me { id } } query Me { users { id } }", id="two-operations-of-one-name"),
        ],
    )
    def test_operations_a_server_cannot_tell_apart_are_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)

    @pytest.mark.parametrize(
        ("value", "reason"),
        [
            pytest.param("{ user { id name }", "Syntax Error: Expected Name, found <EOF>.", id="syntax-error"),
            pytest.param("type User { id: ID }", "The object_type_definition is not executable: a request holds operations and fragments.", id="type-definition"),
            pytest.param("query Me { me { id } } query Me { users { id } }", "There can be only one operation named 'Me'.", id="two-operations-of-one-name"),
            pytest.param("{ me { id } } query Users { users { id } }", "This anonymous operation must be the only defined operation.", id="anonymous-beside-another"),
            # graphql-core 3.2.10 to 3.2.13 and 3.3.0 to 3.3.1 fail printing
            # an error's location after a line separator Python counts and
            # their printer does not: the reason is the error's message alone.
            pytest.param('query A { a(t: "\u2028") } query A { b }', "There can be only one operation named 'A'.", id="after-a-line-separator"),
            pytest.param("# \x0c\nquery A { a } query A { b }", "There can be only one operation named 'A'.", id="after-a-form-feed"),
        ],
    )
    def test_invalid_says_why(self, value, reason):
        with pytest.raises(ValidationError, match=re.escape(f"Invalid GraphQL query: {reason}")):
            validate(GraphQLQuery, value)

    @pytest.mark.parametrize(
        "value",
        [
            # graphql-core 3.2.10 to 3.2.13's lexer runs off the text with an
            # IndexError on these; later versions say a syntax error.
            pytest.param('query Q { a(s: "\\', id="string-ending-in-a-backslash"),
            pytest.param('{ a(s: "\\u1', id="cut-short-unicode-escape"),
            # The parser's message quotes the surrogate, which pydantic
            # cannot encode unescaped, on every version.
            pytest.param('{ a(s: "\\\ud800") }', id="lone-surrogate-after-a-backslash"),
        ],
    )
    def test_whatever_the_parser_raises_is_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)

    @pytest.mark.parametrize("kind", NESTING_KINDS)
    def test_nesting_200_levels_deep_is_valid(self, kind):
        value = _nested(kind, 200)
        assert validate(GraphQLQuery, value) == value

    @pytest.mark.parametrize("kind", NESTING_KINDS)
    def test_nesting_201_levels_deep_is_invalid(self, kind):
        """Refused by the parser of 3.2.14 and 3.3.2, and by the check on
        older versions, which parse it."""
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, _nested(kind, 201))

    def test_valid_with_fragments_from_file(self, datadir):
        query = (datadir / "graphql_with_fragments.graphql").read_text()
        assert validate(GraphQLQuery, query) == query

    # Empty braces that are no selection set: an input object or a list with
    # nothing in it is a value, and braces in a string or a comment are text.
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("{ search(filter: {}) { id } }", id="object-argument"),
            pytest.param("{ search(filter: {tags: {}, ids: []}) { id } }", id="nested-in-an-object-argument"),
            pytest.param("{ search(ids: []) { id } }", id="list-argument"),
            pytest.param("query ($filter: Filter = {}) { search(filter: $filter) { id } }", id="object-variable-default"),
            pytest.param("query ($ids: [ID!] = []) { search(ids: $ids) { id } }", id="list-variable-default"),
            pytest.param("{ user @audit(context: {}) { id } }", id="directive-argument"),
            pytest.param('{ echo(text: "{ user { } }") }', id="string"),
            pytest.param('{ echo(text: """\n  { user { } }\n""") }', id="block-string"),
            pytest.param("{ user { id # { }\n } }", id="comment"),
        ],
    )
    def test_empty_braces_that_are_no_selection_set_are_valid(self, value):
        assert validate(GraphQLQuery, value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "{ user { id name }",
            "not a graphql query",
            # Deeper than any version parses, which one says as a syntax error
            # and another by running into the recursion limit: both are this.
            pytest.param(_nested_selection_sets(BEYOND_RECURSION_LIMIT, "{ id }"), id="nested-too-deep"),
        ],
    )
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)

    # Every place a selection set is written. Commas and comments are ignored,
    # so a set holding only those selects nothing either.
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("{ }", id="anonymous-operation"),
            pytest.param("query GetUser { }", id="query"),
            pytest.param("mutation { }", id="mutation"),
            pytest.param("subscription OnUser { }", id="subscription"),
            pytest.param("query GetUser($id: ID!) @cached { }", id="operation-with-variables-and-directive"),
            pytest.param("{ user { } }", id="field"),
            pytest.param("{ me: user(id: 1) @include(if: true) { } }", id="field-with-alias-arguments-and-directive"),
            pytest.param("{ user { team { lead { } } } }", id="nested-field"),
            pytest.param("{ user { id } team { } }", id="after-a-field-that-selects"),
            pytest.param("{ team { } user { id } }", id="before-a-field-that-selects"),
            pytest.param("{ node { ... on User { } } }", id="inline-fragment"),
            pytest.param("{ node { ... { } } }", id="inline-fragment-without-a-type"),
            pytest.param("{ node { ... @include(if: true) { } } }", id="inline-fragment-with-a-directive"),
            pytest.param("{ user { ...Fields } } fragment Fields on User { }", id="fragment-definition"),
            pytest.param("{ user { ...Fields } } fragment Fields on User { team { } }", id="field-in-a-fragment-definition"),
            pytest.param("query A { user { id } } query B { }", id="second-operation"),
            pytest.param("{ user { , } }", id="only-commas"),
            pytest.param("{ user { # id\n } }", id="only-a-comment"),
            pytest.param("{ search(filter: {}) { } }", id="after-an-empty-object-argument"),
            pytest.param(_nested_selection_sets(149, "{ }"), id="nested-150-deep"),
        ],
    )
    def test_empty_selection_set_is_invalid(self, value):
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate(GraphQLQuery, value)

    @pytest.mark.parametrize(
        ("annotated_type", "value"),
        [
            pytest.param(GraphQLQuery, "query Q { a }", id="query"),
            # The GraphQL body's query field, which holds a templated query to
            # the rules by its stand-in.
            pytest.param(Annotated[GraphQLQuery | PartialTemplateStr, AfterValidator(validate_templated_graphql_query)], "query Q { {{ field }} }", id="templated-query"),
        ],
    )
    def test_a_rule_that_crashes_is_not_the_query_s_error(self, monkeypatch, annotated_type, value):
        """What the parser raises and the rules' own refusals are the query's
        errors; anything else a rule raises is a bug of the rule's, which
        propagates instead of reading as a query the parser could not read."""

        class RuleBug(Exception):
            pass

        def crash(*args: Any) -> bool:
            raise RuleBug

        monkeypatch.setattr("pytest_httpchain.models.types._selects_nothing", crash)
        with pytest.raises(RuleBug):
            validate(annotated_type, value)


class TestTemplateExpression:
    """A complete ``{{ expr }}`` — nothing outside the braces."""

    @pytest.mark.parametrize("value", ["{{ value }}", "{{ foo }}", "{{ a + b }}", "{{ user.name }}", "{{ name | upper }}"])
    def test_valid(self, value):
        assert validate(TemplateExpression, value) == value

    # An escaped template is text, and so is a backslash before one: neither
    # is the value, whatever its type. Nor is a template in escaped text.
    @pytest.mark.parametrize("value", ["prefix {{ value }}", "{{ value }} suffix", "just a string", r"\{{ value }}", r"\\{{ value }}", r"\{{{{ value }}"])
    def test_invalid(self, value):
        with pytest.raises(ValidationError, match="Must be a complete template expression"):
            validate(TemplateExpression, value)


class TestPartialTemplateStr:
    """At least one non-empty ``{{ expr }}`` anywhere in the string."""

    @pytest.mark.parametrize(
        "value",
        [
            "{{ value }}",
            "Hello {{ name }}!",
            "prefix {{ value }} suffix",
            "{{ first }} and {{ second }}",
            # A backslash, then a template; an escaped one beside a template,
            # which the escaped text's `}}` ends.
            r"\\{{ value }}",
            r"\{{ text }} and {{ value }}",
            r"\{{ text }}{{ value }}",
        ],
    )
    def test_valid(self, value):
        assert validate(PartialTemplateStr, value) == value

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ("no template here", "Must contain at least one template expression"),
            ("{{  }}", "Template expression cannot be empty"),
            # Escaped, braces are text: no template, however many.
            (r"\{{ value }}", "Must contain at least one template expression"),
            (r"\{{ a }} and \{{ b }}", "Must contain at least one template expression"),
            (r"\{{  }}", "Must contain at least one template expression"),
            # A template's syntax in escaped text, up to its `}}`, is text too.
            (r"\{{{{ value }}", "Must contain at least one template expression"),
            (r"\{{ a {{ value }} }}", "Must contain at least one template expression"),
        ],
    )
    def test_invalid(self, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(PartialTemplateStr, value)


class TestSerializablePath:
    """A path is a `Path` once its text is final. Text rendering changes (a
    template, an escape) is kept as written, for the one rendering: unescaped
    as it loaded, a `\\{{` made `{{` was then rendered once more, as a
    template, and a `Path` would normalize it before it renders."""

    @pytest.mark.parametrize(
        "written",
        [
            r"files/\{{name}}.bin",
            r"files/\\\{{name}}.bin",
            r"files/\\{{ name }}.bin",
            "files/{{ name }}.bin",
            # A separator before the escape, which Windows would fold into it.
            r"files\\{{name}}.bin",
        ],
    )
    def test_text_rendering_changes_is_kept(self, written):
        assert validate(SerializablePath, written) == written

    # A backslash anywhere but before `{{` is the path's own.
    @pytest.mark.parametrize("written", [r"files\name.bin", "files/name.bin", "files/{name}.bin"])
    def test_final_text_is_a_path(self, written):
        assert validate(SerializablePath, written) == Path(written)

    def test_rendered_path_is_final(self):
        """What a template rendered holds a value as it is: a path a template
        rendered is a `Path`, never unescaped."""
        assert TypeAdapter(SerializablePath).validate_python(r"files/\{{name}}.bin", context=RENDERED) == Path(r"files/\{{name}}.bin")


class TestLiteralJudgedAsRendered:
    """A literal is checked as the text it is used as, its escapes rendered
    (`as_rendered`), and kept as written for the one rendering to unescape:
    judged as written, an escape's backslash read as the grammar's own escape,
    which GraphQL and JMESPath strings have no `\\{` for. A value a template
    rendered (`RENDERED`) is final, judged as it is."""

    @pytest.mark.parametrize(
        ("annotated_type", "value"),
        [
            pytest.param(GraphQLQuery, r'query { render(template: "Hello \{{name}}") }', id="graphql-string"),
            pytest.param(JMESPathExpression, r'"\{{ name }}"', id="jmespath-quoted-identifier"),
            pytest.param(JMESPathExpression, r'items[?name == `"\{{x}}"`]', id="jmespath-json-literal"),
        ],
    )
    def test_escape_in_a_string_of_the_grammar(self, annotated_type, value):
        assert validate(annotated_type, value) == value
        with pytest.raises(ValidationError):
            TypeAdapter(annotated_type).validate_python(value, context=RENDERED)

    @pytest.mark.parametrize(
        ("annotated_type", "value"),
        [
            pytest.param(RegexPattern, r"^\{{(\w+)}}$", id="regex"),
            pytest.param(XMLString, r"<t>\{{name}}</t>", id="xml"),
        ],
    )
    def test_kept_as_written(self, annotated_type, value):
        assert validate(annotated_type, value) == value

    @pytest.mark.parametrize(
        ("annotated_type", "value", "message"),
        [
            # `{{` alone is no base64 either.
            pytest.param(Base64String, r"\{{", "Invalid base64 encoding", id="base64"),
            # Two backslashes before a `{{` that opens no template render one,
            # which escapes nothing in JMESPath. As written, JMESPath read them
            # as one escaped backslash.
            pytest.param(JMESPathExpression, r'"\\{{"', "Invalid JMESPath expression", id="jmespath-doubled"),
        ],
    )
    def test_rendered_text_is_what_fails(self, annotated_type, value, message):
        with pytest.raises(ValidationError, match=message):
            validate(annotated_type, value)


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


class TestSchemaFileRef:
    """A ``body.schema`` file reference: a local file, then an RFC 6901 JSON
    pointer in URI fragment form after its first ``#``."""

    @pytest.mark.parametrize(
        ("ref", "expected"),
        [
            pytest.param("schemas/user.json", SchemaFileRef("schemas/user.json", "", ()), id="no-pointer"),
            pytest.param("user.json#", SchemaFileRef("user.json", "", ()), id="empty-fragment-is-the-whole-document"),
            pytest.param(
                "./openapi.json#/components/schemas/User",
                SchemaFileRef("./openapi.json", "/components/schemas/User", ("components", "schemas", "User")),
                id="openapi-component",
            ),
            # Percent-decoded first, then split, then unescaped: %2F is a
            # separator, ~1 a slash in a key, and ~01 is "~1", not "/".
            pytest.param("a.json#/paths/~1users~1{id}/get", SchemaFileRef("a.json", "/paths/~1users~1{id}/get", ("paths", "/users/{id}", "get")), id="escaped-slash"),
            pytest.param("a.json#/~0x/~01", SchemaFileRef("a.json", "/~0x/~01", ("~x", "~1")), id="escaped-tilde"),
            pytest.param("a.json#/a%20b/c%2Fd/%C3%A9", SchemaFileRef("a.json", "/a%20b/c%2Fd/%C3%A9", ("a b", "c", "d", "é")), id="percent-decoded"),
            # A Path would fold these: "/" is the empty key, "//x" two keys.
            pytest.param("a.json#/", SchemaFileRef("a.json", "/", ("",)), id="empty-key"),
            pytest.param("a.json#//x/", SchemaFileRef("a.json", "//x/", ("", "x", "")), id="empty-keys"),
            pytest.param("a.json#/items/0", SchemaFileRef("a.json", "/items/0", ("items", "0")), id="array-index"),
            # Only the first '#' splits: the pointer may hold one.
            pytest.param("a.json#/a#b", SchemaFileRef("a.json", "/a#b", ("a#b",)), id="hash-in-pointer"),
            # A one-letter scheme is a Windows drive.
            pytest.param("C:\\schemas\\x.json#/a", SchemaFileRef("C:\\schemas\\x.json", "/a", ("a",)), id="windows-drive"),
            # A colon is a path's, unless it starts a URI: these loaded as
            # paths before the field took pointers, and still do.
            pytest.param("schemas:v1/user.json", SchemaFileRef("schemas:v1/user.json", "", ()), id="colon-in-a-directory"),
            pytest.param("user-2024-01-01T10:00.json", SchemaFileRef("user-2024-01-01T10:00.json", "", ()), id="colon-in-a-name"),
            pytest.param("urn:x.json", SchemaFileRef("urn:x.json", "", ()), id="scheme-like-without-authority"),
            # `./` keeps a path that starts like a URI a path.
            pytest.param("./http:x/user.json", SchemaFileRef("./http:x/user.json", "", ()), id="dot-slash-before-a-scheme"),
        ],
    )
    def test_parsed(self, ref, expected):
        assert parse_schema_file_ref(ref) == expected
        assert validate(SchemaFileRefStr, ref) == ref

    @pytest.mark.parametrize(
        ("ref", "message"),
        [
            pytest.param("https://example.com/openapi.json#/components", "remote schemas are not fetched", id="https"),
            pytest.param("HTTP://example.com/s.json", "remote schemas are not fetched", id="http-any-case"),
            pytest.param("file:///srv/s.json", "a local file path, not a URI", id="file-uri"),
            pytest.param("File:s.json", "a local file path, not a URI", id="file-uri-without-slashes"),
            pytest.param("ftp://example.com/s.json", "a local file path, not a URI", id="other-scheme-with-authority"),
            pytest.param("https:s.json", "remote schemas are not fetched", id="https-without-slashes"),
            # The first '#' ends the path, so a file whose name holds one
            # cannot be named (rename it).
            pytest.param("schemas/c#1.json", "must be a JSON pointer starting with '/'", id="hash-in-a-file-name"),
            pytest.param("#/components/schemas/User", "must name a file before its '#'", id="pointer-only"),
            pytest.param("", "must name a file before its '#'", id="empty"),
            pytest.param("a.json#User", "must be a JSON pointer starting with '/'", id="plain-name-anchor"),
            pytest.param("a.json#/a~2b", "must be followed by 0 (for '~') or 1 (for '/')", id="bad-escape"),
            pytest.param("a.json#/a~", "must be followed by 0 (for '~') or 1 (for '/')", id="trailing-tilde"),
            # A tilde spelled %7E is a tilde once decoded: the escape rule holds.
            pytest.param("a.json#/a%7E2", "must be followed by 0 (for '~') or 1 (for '/')", id="encoded-bad-escape"),
            pytest.param("a.json#/%FF", "percent-encoding is not UTF-8", id="not-utf8"),
        ],
    )
    def test_refused(self, ref, message):
        with pytest.raises(ValueError, match=re.escape(message)):
            parse_schema_file_ref(ref)
        with pytest.raises(ValidationError, match=re.escape(message)):
            validate(SchemaFileRefStr, ref)

    def test_path_object_stands_for_its_text(self):
        """A template renders a fixture's Path as it is, which the field took
        when it was a Path."""
        assert validate(SchemaFileRefStr, Path("schemas") / "user.json") == str(Path("schemas") / "user.json")

    def test_template_is_left_to_the_template_branch(self):
        with pytest.raises(ValidationError, match="contains a template expression at position 8"):
            validate(SchemaFileRefStr, "schemas/{{ name }}.json")


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


class TestVarsNamespace:
    """What a `vars` object is to a template: read by attribute, as a
    SimpleNamespace always was, and by key, as a dict saved from a response is."""

    @staticmethod
    def _user() -> Any:
        return convert_dict_to_namespace({"name": "Alice", "Content-Type": "json", "roles": [{"id": 1}], "address": {"city": "Oslo"}})

    def test_every_object_at_every_depth_is_one(self):
        """Nested objects and objects in lists too, so a template reads
        ``user['address']['city']`` and ``user.roles[0]['id']``."""
        user = self._user()
        assert type(user) is VarsNamespace
        assert type(user["address"]) is VarsNamespace
        assert type(user.roles[0]) is VarsNamespace
        assert (user["address"]["city"], user.roles[0]["id"]) == ("Oslo", 1)

    def test_mapping_protocol_reads_the_keys_in_their_order(self):
        user = self._user()
        assert user["Content-Type"] == "json"
        assert "name" in user
        assert "nick" not in user
        assert len(user) == 4
        assert list(user) == ["name", "Content-Type", "roles", "address"]
        assert list(user.keys()) == ["name", "Content-Type", "roles", "address"]
        assert list(user.values())[:2] == ["Alice", "json"]
        assert list(user.items())[:2] == [("name", "Alice"), ("Content-Type", "json")]
        assert (user.get("name"), user.get("nick"), user.get("nick", "anon")) == ("Alice", None, "anon")

    def test_attribute_access_is_unchanged(self):
        assert self._user().name == "Alice"

    def test_missing_key_raises_key_error(self):
        """The template engine names the key in its own message."""
        with pytest.raises(KeyError, match="nick"):
            self._user()["nick"]

    def test_key_named_like_a_method_is_data(self):
        """As an attribute the data wins, as it always did, and shadows the
        method: ``order.items()`` calls the data. Every other form reads the
        key, since the methods read the instance's dict, never an attribute."""
        order = convert_dict_to_namespace({"items": [1, 2], "keys": "k", "get": "g", "values": 0})
        assert (order.items, order.keys, order.get, order.values) == ([1, 2], "k", "g", 0)
        assert order["items"] == [1, 2]
        assert list(order) == ["items", "keys", "get", "values"]
        assert len(order) == 4
        assert "items" in order
        with pytest.raises(TypeError, match="not callable"):
            order.items()
        # The class's own methods still work around the shadowing.
        assert list(VarsNamespace.items(order)) == [("items", [1, 2]), ("keys", "k"), ("get", "g"), ("values", 0)]

    def test_underscore_and_dunder_keys_are_data_only(self):
        """A key is data: ``_id`` reads by key. The class's attributes are
        never reached by key, whatever the key."""
        doc = convert_dict_to_namespace({"_id": 7, "__class__": "shadow"})
        assert doc["_id"] == 7
        assert doc["__class__"] == "shadow"
        assert type(doc) is VarsNamespace
        with pytest.raises(KeyError):
            convert_dict_to_namespace({})["__class__"]

    def test_special_methods_are_the_class_s_whatever_the_keys(self):
        """Python looks special methods up on the class, so data cannot
        replace them."""
        odd = convert_dict_to_namespace({"__getitem__": 1, "__len__": 2, "__iter__": 3})
        assert (odd["__len__"], len(odd), list(odd)) == (2, 3, ["__getitem__", "__len__", "__iter__"])

    def test_equality_is_a_simple_namespace_s(self):
        """Equal to a plain SimpleNamespace of the same members, as before; a
        dict stays unequal, as it was."""
        user = convert_dict_to_namespace({"a": 1, "b": {"c": 2}})
        assert user == SimpleNamespace(a=1, b=SimpleNamespace(c=2))
        assert user == convert_dict_to_namespace({"a": 1, "b": {"c": 2}})
        assert user != convert_dict_to_namespace({"a": 1, "b": {"c": 3}})
        assert user != {"a": 1, "b": {"c": 2}}

    @pytest.mark.parametrize(("value", "expected"), [({}, False), ({"a": 0}, True)], ids=["empty", "non-empty"])
    def test_truth_is_a_dict_s(self, value, expected):
        """An empty object is false, as ``{}`` saved from a response is."""
        assert bool(convert_dict_to_namespace(value)) is expected

    def test_repr_is_a_simple_namespace_s(self):
        """Interpolated into text, an object reads as it always has."""
        ns = convert_dict_to_namespace({"a": 1, "Content-Type": "x", "b": {"c": [1]}, "": "unnamed"})
        assert repr(ns) == repr(SimpleNamespace(**{"a": 1, "Content-Type": "x", "b": SimpleNamespace(c=[1]), "": "unnamed"}))
        assert str(ns) == "namespace(a=1, Content-Type='x', b=namespace(c=[1]))"

    def test_repr_of_an_object_that_contains_itself(self):
        loop = VarsNamespace(a=1)
        loop.self = loop
        assert repr(loop) == "namespace(a=1, self=namespace(...))"

    @staticmethod
    def _both(build: Any) -> tuple[Any, Any]:
        """``build(namespace_type)`` for this class and for SimpleNamespace,
        whose C repr is the text to match."""
        return build(VarsNamespace), build(SimpleNamespace)

    @staticmethod
    def _list_cycle(ns: Any) -> Any:
        items: list[Any] = [1]
        obj = ns(items=items)
        items.extend([items, obj])
        return obj

    @staticmethod
    def _dict_cycle(ns: Any) -> Any:
        data: dict[str, Any] = {}
        obj = ns(data=data)
        data.update(data=data, obj=obj)
        return obj

    @staticmethod
    def _shared(ns: Any) -> Any:
        shared = ns(x=1)
        return ns(a=shared, b=shared, c=[shared, shared])

    @pytest.mark.parametrize(
        "build",
        [
            # A template can render any of these into an object.
            pytest.param(lambda ns: ns(d={"k": ns(a=1), 2: (ns(),)}, one=(1,), none=(), pair=(1, "b"), nested=[[], [{}]]), id="rendered-dicts-and-tuples"),
            pytest.param(_list_cycle, id="list-that-contains-itself"),
            pytest.param(_dict_cycle, id="dict-that-contains-itself"),
            # Met again, but not inside itself: written again, as the C reprs do.
            pytest.param(_shared, id="shared-not-a-cycle"),
            pytest.param(lambda ns: ns(**{"a": b"x", "f": 1.5, "n": None, "t": True, "s": "it's"}), id="scalars"),
        ],
    )
    def test_repr_writes_the_c_repr_s_text(self, build):
        ours, plain = self._both(build)
        assert repr(ours) == repr(plain)

    def test_repr_of_an_object_nested_hundreds_deep(self):
        """SimpleNamespace's C repr wrote an object the loader accepts at this
        depth; a Python repr recursing per level spent three frames on each
        and overflowed, so a verify expression rendering one failed as a bare
        RecursionError and text interpolating one failed the stage."""
        loaded = convert_dict_to_namespace({"deep": nested("x", LOADABLE_BUT_DEEP)})

        def build(ns: Any) -> Any:
            value: Any = "x"
            for level in range(LOADABLE_BUT_DEEP):
                value = [value] if level % 2 else ns(k=value)
            return ns(deep=value)

        ours, plain = self._both(build)
        assert repr(loaded) == repr(ours) == repr(plain)

    def test_repr_far_past_the_recursion_limit(self):
        value: Any = "x"
        for _ in range(BEYOND_RECURSION_LIMIT):
            value = VarsNamespace(k=[value])
        assert repr(value).startswith("namespace(k=[namespace(k=[")

    def test_repr_through_another_object_s_repr(self):
        """An object written by its own repr that holds the namespace writing
        it: the guard is the thread's, not the loop's."""

        class Box:
            def __init__(self, value: Any) -> None:
                self.value = value

            def __repr__(self) -> str:
                return f"Box({self.value!r})"

        obj = VarsNamespace(a=1)
        obj.box = Box(obj)
        assert repr(obj) == "namespace(a=1, box=Box(namespace(...)))"

    def test_repr_that_raises_leaves_no_guard_behind(self):
        """Else the object would read ``namespace(...)`` from then on."""

        class Unwritable:
            def __repr__(self) -> str:
                raise ValueError("no repr")

        obj = VarsNamespace(a=1)
        obj.bad = Unwritable()
        with pytest.raises(ValueError, match="no repr"):
            repr(obj)
        del obj.bad
        assert repr(obj) == "namespace(a=1)"

    def test_pickles_with_its_type(self):
        """Namespaces may cross into xdist's reports or a process pool."""
        user = self._user()
        copy = pickle.loads(pickle.dumps(user))
        assert copy == user
        assert (type(copy), type(copy.address), type(copy.roles[0])) == (VarsNamespace, VarsNamespace, VarsNamespace)

    def test_shared_by_threads(self):
        """Parallel iterations read one object at once: the methods only read."""
        user = self._user()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: (user["name"], list(user), dict(user.items())["Content-Type"]), range(64)))
        assert results == [("Alice", ["name", "Content-Type", "roles", "address"], "json")] * 64

    def test_is_a_simple_namespace_and_a_mapping(self):
        user = self._user()
        assert isinstance(user, SimpleNamespace)
        assert isinstance(user, Mapping)
        assert dict(user) == {"name": "Alice", "Content-Type": "json", "roles": user.roles, "address": user.address}

    @pytest.mark.parametrize("annotated_type", [list[Any], list[str], tuple[str, ...], set[str]], ids=["list", "list-str", "tuple", "set"])
    def test_refused_where_a_list_goes_as_a_dict_is(self, annotated_type):
        """pydantic's lax list takes any iterable but a mapping: as a plain
        iterable, an object rendered where a list goes (a parameter's values)
        was taken for the list of its keys."""
        with pytest.raises(ValidationError, match="Input should be a valid (list|tuple|set)"):
            validate(annotated_type, self._user())

    def test_taken_where_an_object_goes_as_a_dict_is(self):
        assert validate(dict[str, Any], convert_dict_to_namespace({"a": 1})) == {"a": 1}

    def test_converts_to_json_data_as_a_simple_namespace(self):
        user = self._user()
        assert convert_namespace_to_dict(user) == {"name": "Alice", "Content-Type": "json", "roles": [{"id": 1}], "address": {"city": "Oslo"}}
        assert json.loads(json.dumps(convert_namespace_to_dict(user))) == convert_namespace_to_dict(user)

    def test_one_object_is_not_a_sequence_of_items(self):
        """Iterable over its keys, but a mapping: not iterated for them."""
        user = self._user()
        assert convert_namespace_items_to_dict(user) is user
        assert convert_namespace_items_to_dict((user,)) == [dict(vars(user))]
