"""Unit tests for all RequestBody types."""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    Base64Body,
    BinaryBody,
    BytesBody,
    FilesBody,
    FileSpec,
    FormBody,
    GraphQL,
    GraphQLBody,
    JsonBody,
    MsgpackBody,
    Multipart,
    MultipartBody,
    Request,
    TextBody,
    XmlBody,
    validate_rendered,
)
from tests.unit.models.helpers import assert_error_types


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param({"json": {"data": "test"}}, JsonBody, id="json"),
        pytest.param({"xml": "<root/>"}, XmlBody, id="xml"),
        pytest.param({"form": {"key": "value"}}, FormBody, id="form"),
        pytest.param({"text": "content"}, TextBody, id="text"),
        pytest.param({"base64": "dGVzdA=="}, Base64Body, id="base64"),
        pytest.param({"binary": "file.bin"}, BinaryBody, id="binary"),
        pytest.param({"bytes": "{{ msgpack_pack(packet) }}"}, BytesBody, id="bytes"),
        pytest.param({"msgpack": {"b": "{{ hex_bytes('00ff') }}"}}, MsgpackBody, id="msgpack"),
        pytest.param({"files": {"f": "file.txt"}}, FilesBody, id="files"),
        pytest.param({"multipart": {"fields": {"f": "v"}}}, MultipartBody, id="multipart"),
        pytest.param({"graphql": {"query": "{ test }"}}, GraphQLBody, id="graphql"),
    ],
)
def test_raw_body_dict_selects_model(body, expected):
    """The body key picks the model — the path every scenario file takes."""
    request = Request.model_validate({"url": "https://example.com", "body": body})
    assert type(request.body) is expected


def test_multiple_body_types_not_allowed():
    """With several body keys the discriminator picks one deterministically
    (the alphabetically first, here base64) and that model rejects the rest."""
    with pytest.raises(ValidationError) as exc_info:
        Request(url="https://example.com", body={"text": "content", "base64": "ZW5j"})
    assert_error_types(exc_info, "extra_forbidden", at="text")


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        pytest.param(TextBody, "text", "Hello, World!", id="text"),
        pytest.param(JsonBody, "json", {"users": [{"name": "Alice"}, {"name": "Bob"}], "count": 2}, id="json-object"),
        pytest.param(JsonBody, "json", [1, 2, 3], id="json-array"),
        pytest.param(JsonBody, "json", "string", id="json-string"),
        pytest.param(JsonBody, "json", 42, id="json-int"),
        pytest.param(JsonBody, "json", 3.14, id="json-float"),
        pytest.param(JsonBody, "json", True, id="json-bool"),
        pytest.param(JsonBody, "json", None, id="json-null"),
        pytest.param(FormBody, "form", {"string": "value", "number": 42, "boolean": True}, id="form"),
        pytest.param(FormBody, "form", {}, id="form-empty"),
    ],
)
def test_concrete_value_round_trips(model, field, value):
    assert getattr(model(**{field: value}), field) == value


def test_graphql_variables_round_trip():
    assert GraphQL(query="query GetUser($id: ID!) { user(id: $id) { name } }", variables={"id": "123"}).variables == {"id": "123"}


@pytest.mark.parametrize("filename", ["mutation_create_user.graphql", "query_with_variables.graphql"])
def test_graphql_query_from_file_accepted_verbatim(datadir, filename):
    """Multi-line documents with input objects and directives parse and are kept as-is."""
    query = (datadir / filename).read_text()
    assert GraphQLBody(graphql={"query": query}).graphql.query == query


@pytest.mark.parametrize(
    ("model", "field", "value", "expected"),
    [
        pytest.param(BinaryBody, "binary", "csvs/mydata.csv", Path("csvs/mydata.csv"), id="binary-str"),
        pytest.param(BinaryBody, "binary", Path("data/file.bin"), Path("data/file.bin"), id="binary-path"),
        pytest.param(FilesBody, "files", {"doc1": "file1.pdf", "doc2": "file2.pdf"}, {"doc1": Path("file1.pdf"), "doc2": Path("file2.pdf")}, id="files-str"),
        pytest.param(FilesBody, "files", {"file": Path("data/upload.bin")}, {"file": Path("data/upload.bin")}, id="files-path"),
    ],
)
def test_path_fields_become_paths(model, field, value, expected):
    assert getattr(model(**{field: value}), field) == expected


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        # A path an escape is rendered out of is kept as written, for the
        # one rendering: unescaped here, its braces were rendered again.
        pytest.param(BinaryBody, "binary", r"data/\{{name}}.bin", id="binary-escaped"),
        pytest.param(FilesBody, "files", {"file": r"data/\{{name}}.bin"}, id="files-escaped"),
        pytest.param(FilesBody, "files", {"file": {"path": r"data/\{{name}}.bin"}}, id="file-spec-escaped"),
    ],
)
def test_path_rendering_changes_is_kept_as_written(model, field, value):
    assert model(**{field: value}).model_dump(mode="json", exclude_none=True)[field] == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # What rendering made of the scenario's text is final: a Path.
        pytest.param("data/name.bin", Path("data/name.bin"), id="plain"),
        # A value a template put in, a `\{{` included, is never unescaped.
        pytest.param(r"data/\{{name}}.bin", Path(r"data/\{{name}}.bin"), id="escape-a-value-holds"),
    ],
)
def test_path_a_template_rendered_is_final(value, expected):
    assert validate_rendered(BinaryBody, {"binary": value}).binary == expected


@pytest.mark.parametrize(
    ("model", "field", "value"),
    [
        pytest.param(TextBody, "text", "prefix {{ value }} suffix", id="text"),
        pytest.param(Base64Body, "base64", "{{ encoded_data }}", id="base64"),
        pytest.param(Base64Body, "base64", "prefix{{ value }}", id="base64-partial"),
        pytest.param(BinaryBody, "binary", "{{ file_path }}", id="binary"),
        pytest.param(BinaryBody, "binary", "data/{{ filename }}.csv", id="binary-partial"),
        pytest.param(FilesBody, "files", {"file": "uploads/{{ filename }}"}, id="files"),
        pytest.param(XmlBody, "xml", "{{ payload }}", id="xml"),
        pytest.param(GraphQL, "query", "{{ graphql_query }}", id="graphql-query"),
    ],
)
def test_template_kept_as_unvalidated_str(model, field, value):
    """A template skips content validation (base64/XML/GraphQL) and path
    coercion; it is validated after rendering instead."""
    assert getattr(model(**{field: value}), field) == value


@pytest.mark.parametrize(
    ("construct", "message"),
    [
        pytest.param(lambda: Base64Body(base64="not-valid-base64!!!"), "Invalid base64 encoding", id="base64"),
        pytest.param(lambda: XmlBody(xml="<root>unclosed"), "Invalid XML", id="xml"),
        pytest.param(lambda: GraphQL(query="{ user { id name }"), "Invalid GraphQL query", id="graphql"),
    ],
)
def test_invalid_content_rejected(construct, message):
    """Wiring only: the exhaustive cases live in test_type_validators.py."""
    with pytest.raises(ValidationError, match=message):
        construct()


def test_graphql_query_a_template_rendered_is_valid():
    """A query a template rendered is validated again, as the `GraphQL` it
    goes into, when its stage runs (`validate_rendered`, as the carrier does):
    an empty input object is a value, not a selection set."""
    query = "{ search(filter: {}) { id } }"
    assert validate_rendered(GraphQL, {"query": query}).query == query


@pytest.mark.parametrize("query", [pytest.param("{ user { } }", id="field"), pytest.param("{ }", id="operation")])
def test_graphql_query_a_template_rendered_selecting_nothing_is_invalid(query):
    """Refused as a query the scenario wrote out is, whichever graphql-core
    is installed: 3.3.2 parses an empty selection set, older versions do not."""
    with pytest.raises(ValidationError, match="Invalid GraphQL query"):
        validate_rendered(GraphQL, {"query": query})


def test_json_namespace_becomes_dict():
    """A rendered ``{{ var }}`` may be a SimpleNamespace (vars are namespaces);
    JSON-serialized fields turn it back into a plain dict, recursively."""
    body = JsonBody(json=SimpleNamespace(a=1, b=[SimpleNamespace(c=2)], d={"e": SimpleNamespace(f=3)}))
    assert body.json == {"a": 1, "b": [{"c": 2}], "d": {"e": {"f": 3}}}


TWO_OPERATIONS = "query Me { me { id } } query Users { users { id } }"


@pytest.mark.parametrize(
    ("query", "operation_name"),
    [
        # One operation is the one executed, named or not.
        pytest.param("query Me { me { id } }", None, id="one-operation"),
        pytest.param("query Me { me { id } }", "Me", id="one-operation-named"),
        pytest.param("{ me { ...Fields } } fragment Fields on User { id }", None, id="anonymous-operation-with-a-fragment"),
        pytest.param(TWO_OPERATIONS, "Users", id="several-operations-one-named"),
        # The query as it renders, its escape gone, holds the operations.
        pytest.param('query Me { echo(text: "\\{{ x }}") } query Users { users { id } }', "Users", id="escaped-template-in-the-query"),
        # Known once rendered: checked then.
        pytest.param(TWO_OPERATIONS, "{{ operation }}", id="templated-name"),
        pytest.param("{{ query }}", "Users", id="templated-query"),
    ],
)
def test_graphql_operation_name_valid(query, operation_name):
    assert GraphQL(query=query, operation_name=operation_name).operation_name == operation_name


@pytest.mark.parametrize(
    ("query", "operation_name", "message"),
    [
        # What a server answers: "Must provide operation name if query
        # contains multiple operations.", "Unknown operation named 'Them'."
        pytest.param(TWO_OPERATIONS, None, "the query defines several operations ('Me', 'Users'): name the one to execute in operation_name", id="several-operations-unnamed"),
        pytest.param(TWO_OPERATIONS, "Them", "operation_name 'Them' names no operation in the query (it defines 'Me', 'Users')", id="unknown-name"),
        pytest.param("{ me { id } }", "Me", "operation_name 'Me' names no operation in the query (it defines an anonymous one)", id="name-for-an-anonymous-operation"),
        pytest.param(
            'query Me { echo(text: "\\{{ x }}") } query Users { users { id } }', None, "the query defines several operations ('Me', 'Users')", id="escaped-template-in-the-query"
        ),
        pytest.param("query Me { me { id } }", "1st", "Invalid GraphQL operation name '1st'", id="not-a-name"),
    ],
)
def test_graphql_operation_name_invalid(query, operation_name, message):
    with pytest.raises(ValidationError, match=re.escape(message)):
        GraphQL(query=query, operation_name=operation_name)


@pytest.mark.parametrize(
    ("operation_name", "message"),
    [
        pytest.param(None, "the query defines several operations", id="unnamed"),
        pytest.param("Them", "operation_name 'Them' names no operation", id="unknown-name"),
    ],
)
def test_graphql_operation_name_a_template_rendered_is_checked(operation_name, message):
    """Rendered, the query and name are checked as the carrier validates
    them (`validate_rendered`), as written ones are at load."""
    assert validate_rendered(GraphQL, {"query": TWO_OPERATIONS, "operation_name": "Users"}).operation_name == "Users"
    with pytest.raises(ValidationError, match=re.escape(message)):
        validate_rendered(GraphQL, {"query": TWO_OPERATIONS, "operation_name": operation_name})


# A query written with an escape, `\{{ x }}`, renders to literal braces.
_RENDERED_BRACES = 'query Me { echo(text: "{{ x }}") } query Users { users { id } }'


@pytest.mark.parametrize(
    ("operation_name", "message"),
    [
        pytest.param(None, "the query defines several operations ('Me', 'Users')", id="unnamed"),
        pytest.param("Them", "operation_name 'Them' names no operation in the query (it defines 'Me', 'Users')", id="unknown-name"),
    ],
)
def test_graphql_operation_name_is_checked_once_rendered_whatever_braces_the_text_holds(operation_name, message):
    """Rendered text is final: the braces an escape left are text, not a
    template, so the query and name are still checked."""
    assert validate_rendered(GraphQL, {"query": _RENDERED_BRACES, "operation_name": "Users"}).operation_name == "Users"
    with pytest.raises(ValidationError, match=re.escape(message)):
        validate_rendered(GraphQL, {"query": _RENDERED_BRACES, "operation_name": operation_name})


@pytest.mark.parametrize(
    ("query", "message"),
    [
        pytest.param('{ echo(text: "{{ x }}") } query Users { users { id } }', "This anonymous operation must be the only defined operation.", id="anonymous-beside-another"),
        pytest.param('query Users { echo(text: "{{ x }}") } query Users { users { id } }', "There can be only one operation named 'Users'.", id="two-of-one-name"),
    ],
)
def test_graphql_operations_rendered_text_holds_are_told_apart_as_a_server_does(query, message):
    """Rendered text the query's own check did not read, its braces an
    escape left, is still held to the operation rules: naming one does not
    make a server execute it."""
    with pytest.raises(ValidationError, match=re.escape(f"Invalid GraphQL query: {message}")):
        validate_rendered(GraphQL, {"query": query, "operation_name": "Users"})


def test_graphql_rendered_text_that_is_no_graphql_is_not_checked_for_its_operation():
    """Text a template rendered that holds template syntax and is no
    GraphQL is taken as such, as the query field takes it, and sent as it is."""
    assert validate_rendered(GraphQL, {"query": "{{ not graphql }}", "operation_name": "Users"}).operation_name == "Users"


def test_graphql_variables_namespace_becomes_dict():
    assert GraphQL(query="{ a }", variables=SimpleNamespace(id="1")).variables == {"id": "1"}


def _nested_query_holding_a_template(levels: int) -> str:
    """Two operations named A, the first nested ``levels`` deep by selection
    sets, its innermost field taking a template in a string."""
    return "query A " + "{ a " * (levels - 1) + '{ b(x: "{{ id }}") }' + " }" * (levels - 1) + " query A { c }"


def _query_of_twelve_templates(second: str, third: str) -> str:
    """Three operations, the second named ``second`` and the third
    ``third``, around ten templates in strings: t0 in the first, t2 to t10 in
    the second (t1 and t11 are for the names, in the order they are written)."""
    strings = ", ".join(f'y{index}: "{{{{ t{index} }}}}"' for index in range(2, 11))
    return f'query X {{ a(x: "{{{{ t0 }}}}") }} query {second} {{ b({strings}) }} query {third} {{ c }}'


class TestGraphQLQueryHoldingTemplates:
    """A query holding templates is checked when the scenario loads by a
    stand-in, each template written as a name of its own: what a template
    rendering a name, a value or a string's text parses as. An error the
    stand-in has is one the rendered query cannot help having, so each query
    refused here also fails once rendered (`validate_rendered`, as its stage
    validates it), here as a template might render it. A template that
    renders structure leaves a stand-in that does not parse, which tells
    nothing: that query is checked once rendered only, as before."""

    @pytest.mark.parametrize(
        ("query", "message", "rendered"),
        [
            # The query's rules, the template in a string.
            pytest.param(
                'type T { a: Int } query A { a(x: "{{ id }}") }',
                "The object_type_definition is not executable: a request holds operations and fragments.",
                'type T { a: Int } query A { a(x: "7") }',
                id="type-system-definition",
            ),
            # Refused whichever graphql-core is installed: 3.3.2 parses an
            # empty selection set, older versions do not.
            pytest.param('query A { a(x: "{{ id }}") { } }', "Syntax Error: A selection set must not be empty.", 'query A { a(x: "7") { } }', id="empty-selection-set"),
            # A comment in the braces selects nothing, and an empty input
            # object beside them is a value, not a selection set.
            pytest.param(
                'query A($v: In = {}) { a(x: $v, y: "{{ id }}") { # none\n } }',
                "Syntax Error: A selection set must not be empty.",
                'query A($v: In = {}) { a(x: $v, y: "7") { # none\n } }',
                id="empty-selection-set-beside-an-empty-input-object",
            ),
            pytest.param(
                'fragment F on T { a(x: "{{ id }}") }',
                "A request holds at least one operation: fragments alone execute nothing.",
                'fragment F on T { a(x: "7") }',
                id="fragments-alone",
            ),
            pytest.param(
                '{ a(x: "{{ id }}") } query B { b }', "This anonymous operation must be the only defined operation.", '{ a(x: "7") } query B { b }', id="anonymous-beside-another"
            ),
            pytest.param('query A { a(x: "{{ id }}") } query A { b }', "There can be only one operation named 'A'.", 'query A { a(x: "7") } query A { b }', id="two-of-one-name"),
            # The template elsewhere: a value, a field, a type, a variable, an
            # operation's name, which is still one beside an anonymous one.
            pytest.param("query A { a(x: {{ id }}) } query A { b }", "There can be only one operation named 'A'.", "query A { a(x: 7) } query A { b }", id="template-as-a-value"),
            pytest.param("{ {{ field }} } query B { b }", "This anonymous operation must be the only defined operation.", "{ me } query B { b }", id="template-as-a-field"),
            pytest.param(
                "query A($v: {{ type }}) { a(x: $v) } query A { b }",
                "There can be only one operation named 'A'.",
                "query A($v: Int) { a(x: $v) } query A { b }",
                id="template-as-a-type",
            ),
            pytest.param(
                "query A { a @include(if: ${{ flag }}) } query A { b }",
                "There can be only one operation named 'A'.",
                "query A { a @include(if: $on) } query A { b }",
                id="template-as-a-variable",
            ),
            pytest.param(
                "query {{ name }} { a } { b }", "This anonymous operation must be the only defined operation.", "query Me { a } { b }", id="template-as-an-operation-name"
            ),
            # Names templates write, wholly or in part, are told apart from
            # each other and from the names written out, which still clash.
            pytest.param(
                "query {{ x }} { a } query A{{ x }} { b } query A { c } query A { d }",
                "There can be only one operation named 'A'.",
                "query B { a } query AB { b } query A { c } query A { d }",
                id="templated-names-apart-from-names-written-out",
            ),
            # An escaped `\{{` is text, rendered as in a query written out.
            pytest.param(
                'query A { a(x: "\\{{ lit }}", y: "{{ id }}") } query A { b }',
                "There can be only one operation named 'A'.",
                'query A { a(x: "{{ lit }}", y: "7") } query A { b }',
                id="escaped-braces-in-a-string",
            ),
            # A stand-in adds no nesting: 200 levels are the limit's own.
            pytest.param(
                _nested_query_holding_a_template(200),
                "There can be only one operation named 'A'.",
                _nested_query_holding_a_template(200).replace("{{ id }}", "7"),
                id="nested-200-levels-deep",
            ),
        ],
    )
    def test_query_refused_at_load_as_once_rendered(self, query, message, rendered):
        """One error, at the query, in the words of a query written out."""
        with pytest.raises(ValidationError) as exc_info:
            GraphQL(query=query)
        assert [(error["loc"], error["msg"]) for error in exc_info.value.errors()] == [(("query",), f"Value error, Invalid GraphQL query: {message}")]
        # Once rendered, the parser's words for some differ by version.
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate_rendered(GraphQL, {"query": rendered})

    @pytest.mark.parametrize(
        ("query", "operation_name", "message", "rendered"),
        [
            pytest.param(
                'query A { a(x: "{{ id }}") } query B { b }',
                None,
                "the query defines several operations ('A', 'B'): name the one to execute in operation_name",
                'query A { a(x: "7") } query B { b }',
                id="several-operations-unnamed",
            ),
            pytest.param(
                "query A { a(x: {{ id }}) } query B { b }",
                "C",
                "operation_name 'C' names no operation in the query (it defines 'A', 'B')",
                "query A { a(x: 7) } query B { b }",
                id="unknown-name",
            ),
            pytest.param(
                "{ a(x: {{ id }}) }",
                "Me",
                "operation_name 'Me' names no operation in the query (it defines an anonymous one)",
                "{ a(x: 7) }",
                id="name-for-an-anonymous-operation",
            ),
            # A name written out like a stand-in's is the query's own.
            pytest.param(
                'query __httpchain0__httpchain { a(x: "{{ id }}") } query B { b }',
                "C",
                "operation_name 'C' names no operation in the query (it defines '__httpchain0__httpchain', 'B')",
                'query __httpchain0__httpchain { a(x: "7") } query B { b }',
                id="name-written-out-like-a-stand-in",
            ),
        ],
    )
    def test_operation_name_refused_at_load_as_once_rendered(self, query, operation_name, message, rendered):
        """One error, the model's, as for a query written out."""
        with pytest.raises(ValidationError) as exc_info:
            GraphQL(query=query, operation_name=operation_name)
        assert [(error["loc"], error["msg"]) for error in exc_info.value.errors()] == [((), f"Value error, {message}")]
        with pytest.raises(ValidationError, match=re.escape(message)):
            validate_rendered(GraphQL, {"query": rendered, "operation_name": operation_name})

    @pytest.mark.parametrize(
        ("query", "operation_name"),
        [
            # The stand-in breaks no rule.
            pytest.param('query A { a(x: "{{ id }}", y: {}) } query B { b }', "B", id="valid"),
            pytest.param("query A($v: In = {}) { a(x: $v, y: {{ id }}) { b } }", None, id="empty-input-objects-beside-a-template"),
            # An operation's name holding a template may render the name asked
            # for, wholly or in part; with none asked for, the rule is not
            # applied either (the names it gives would be the stand-ins').
            pytest.param("query {{ name }} { a } query B { b }", "C", id="templated-operation-name"),
            pytest.param("query Get{{ kind }} { a } query B { b }", "GetUser", id="partly-templated-operation-name"),
            pytest.param("query {{ name }} { a } query B { b }", None, id="templated-operation-name-none-asked-for"),
            # The name asked for is known once rendered.
            pytest.param('query A { a(x: "{{ id }}") } query B { b }', "{{ operation }}", id="templated-name-asked-for"),
            # Names templates write are told apart: numbered in order alone,
            # the 2nd template's followed by a digit (`1`, `1`) would spell
            # the 12th's (`11`).
            pytest.param("query {{ a }} { a } query {{ b }} { b }", "{{ operation }}", id="templated-names"),
            pytest.param(_query_of_twelve_templates("{{ t1 }}1", "{{ t11 }}"), "{{ operation }}", id="templated-names-a-digit-after-one"),
            # The stand-in tells nothing where a template renders structure: a
            # whole document, a keyword, punctuation, a number's digits, text
            # after a backslash in a string (which may escape it).
            pytest.param("{{ document }}", "A", id="whole-document"),
            pytest.param("{{ kind }} A { a } query A { b }", None, id="keyword"),
            pytest.param("query A { a(x: 1) {{ close }} query A { b }", None, id="punctuation"),
            pytest.param("query A { a(x: 1{{ digits }}) } query A { b }", None, id="number"),
            pytest.param('query A { a(x: "\\\\{{ escaped }}") } query A { b }', None, id="after-a-backslash"),
            # Nor where it nests past the limit, which graphql-core 3.2.14 and
            # 3.3.2 do not parse: the verdict is the same on every version.
            pytest.param(_nested_query_holding_a_template(201), None, id="nested-201-levels-deep"),
        ],
    )
    def test_query_loads_where_its_stand_in_breaks_no_rule_or_tells_nothing(self, query, operation_name):
        assert GraphQL(query=query, operation_name=operation_name).query == query

    @pytest.mark.parametrize(
        "rendered",
        [
            pytest.param("query A { a } query A { b }", id="keyword"),
            pytest.param("query A { a(x: 1) } query A { b }", id="punctuation"),
            pytest.param("query A { a(x: 12) } query A { b }", id="number"),
            pytest.param('query A { a(x: "\\n") } query A { b }', id="after-a-backslash"),
            pytest.param(_nested_query_holding_a_template(201).replace("{{ id }}", "7"), id="nested-201-levels-deep"),
        ],
    )
    def test_query_its_stand_in_tells_nothing_of_fails_once_rendered(self, rendered):
        """Those above whose stand-in tells nothing, as their templates might
        render, fail as their stage runs."""
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            validate_rendered(GraphQL, {"query": rendered})

    def test_rendered_text_is_not_read_for_templates(self):
        """Once rendered, a query is final: braces a value put in it are no
        template, and text that is no GraphQL is sent as it is."""
        query = "query A { a(x: {{ x }}) } query A { b }"
        assert validate_rendered(GraphQL, {"query": query}).query == query


class TestMultipart:
    """``body.multipart`` and the file forms ``body.files`` shares with it."""

    def test_fields_and_files_forms(self):
        """Every field value form and every file form, as the model keeps
        them: literal paths become paths, a file object a `FileSpec`, a list
        a list of either."""
        multipart = Multipart.model_validate(
            {
                "fields": {"title": "Report", "tags": ["a", 2, 1.5, True], "draft": False},
                "files": {
                    "document": "./report.pdf",
                    "images": ["a.png", {"path": "b.png", "filename": "photo.png", "content_type": "image/png"}],
                    "note": {"content": "text", "filename": "note.txt"},
                    "blob": {"base64": "iVBORw0KGgo="},
                },
            }
        )
        assert multipart.fields == {"title": "Report", "tags": ["a", 2, 1.5, True], "draft": False}
        assert multipart.files == {
            "document": Path("report.pdf"),
            "images": [Path("a.png"), FileSpec(path=Path("b.png"), filename="photo.png", content_type="image/png")],
            "note": FileSpec(content="text", filename="note.txt"),
            "blob": FileSpec(base64="iVBORw0KGgo="),
        }

    @pytest.mark.parametrize("key", ["fields", "files"])
    def test_one_of_fields_and_files_is_enough(self, key):
        """Either alone is a body, even empty: a form without inputs."""
        assert Multipart.model_validate({key: {}}).model_fields_set == {key}

    def test_files_body_takes_the_same_file_forms(self):
        """A string path is what it was; objects and lists are new."""
        body = FilesBody.model_validate({"files": {"doc": "a.txt", "more": ["b.txt", {"content": "c"}]}})
        assert body.files == {"doc": Path("a.txt"), "more": [Path("b.txt"), FileSpec(content="c")]}

    @pytest.mark.parametrize(
        "multipart",
        [
            pytest.param({"fields": {"title": "{{ title }}", "tags": "{{ tags }}"}}, id="field-values"),
            pytest.param({"files": {"doc": "{{ path }}", "images": "{{ images }}", "list": ["{{ a }}", "b/{{ name }}.png"]}}, id="file-paths"),
            pytest.param(
                {"files": {"doc": {"path": "{{ p }}", "content_type": "{{ ct }}", "filename": "{{ fn }}"}, "b": {"base64": "{{ data }}"}, "c": {"content": "{{ text }}"}}},
                id="file-object-fields",
            ),
        ],
    )
    def test_template_kept_as_unvalidated_str(self, multipart):
        """A template stands where any value goes, and is checked once rendered."""
        assert Multipart.model_validate(multipart).model_dump(exclude_unset=True, mode="json") == multipart

    @pytest.mark.parametrize(
        ("multipart", "message"),
        [
            pytest.param({}, "A multipart body sets at least one of: fields, files", id="neither-fields-nor-files"),
            # One sentence for what no member of a union of pydantic's strict
            # types takes, instead of one error per member.
            pytest.param({"fields": {"a": None}}, "A multipart field is text, a number or a boolean, or a list of them, got null", id="field-null"),
            pytest.param({"fields": {"a": {"k": "v"}}}, "A multipart field is text, a number or a boolean, or a list of them, got an object", id="field-object"),
            pytest.param({"fields": {"a": ["x", ["y"]]}}, "A multipart field's list holds text, numbers or booleans, got list at [1]", id="field-list-in-list"),
            pytest.param({"files": {"a": {"path": "x", "content": "y"}}}, "A file object sets exactly one of: path, content, base64, got path and content", id="two-sources"),
            pytest.param({"files": {"a": {"filename": "x.txt"}}}, "A file object sets exactly one of: path, content, base64", id="no-source"),
            pytest.param({"files": {"a": None}}, "A file is a path or a file object, got null", id="file-null"),
            pytest.param({"files": {"a": ["x", None]}}, "A file is a path or a file object, got null", id="file-null-in-list"),
            # Written into the part's headers as given: a newline would end
            # the header early and start another.
            pytest.param(
                {"files": {"a": {"content": "x", "content_type": "text/plain\r\nX-Injected: 1"}}},
                "A content type must not contain a control character, got '\\r' at position 10",
                id="content-type-newline",
            ),
        ],
    )
    def test_invalid_rejected(self, multipart, message):
        with pytest.raises(ValidationError) as exc_info:
            Multipart.model_validate(multipart)
        assert [error["msg"] for error in exc_info.value.errors()] == [f"Value error, {message}"]

    @pytest.mark.parametrize(
        ("files", "error", "at"),
        [
            # A list in a list: a list holds files, not lists.
            pytest.param({"a": [["x"]]}, "union_tag_invalid", 0, id="list-in-list"),
            pytest.param({"a": {"content": "x", "content_type": ""}}, "string_too_short", "content_type", id="empty-content-type"),
            pytest.param({"a": {"content": "x", "size": 1}}, "extra_forbidden", "size", id="unknown-key"),
            pytest.param({"a": {"base64": "not base64!"}}, "value_error", "base64", id="bad-base64"),
        ],
    )
    def test_invalid_file_rejected(self, files, error, at):
        with pytest.raises(ValidationError) as exc_info:
            Multipart.model_validate({"files": files})
        assert_error_types(exc_info, error, at=at)

    def test_rendered_values_are_taken_as_declared(self):
        """What templates render: a tuple is a list (a set, unordered, is
        not), a ``vars`` object a file object, a path object a path. A tuple
        of ``vars`` objects is the list of file objects it stands for: the
        namespace conversion walks lists only, so each item converts its own."""
        note = SimpleNamespace(content="x", filename="n.txt")
        multipart = Multipart.model_validate(
            {
                "fields": {"tags": ("a", "b")},
                "files": {"note": note, "doc": Path("d.pdf"), "more": (Path("e.pdf"),), "notes": (note, SimpleNamespace(path="f.png"))},
            }
        )
        assert multipart.fields == {"tags": ["a", "b"]}
        assert multipart.files == {
            "note": FileSpec(content="x", filename="n.txt"),
            "doc": Path("d.pdf"),
            "more": [Path("e.pdf")],
            "notes": [FileSpec(content="x", filename="n.txt"), FileSpec(path=Path("f.png"))],
        }
        assert FilesBody.model_validate({"files": {"notes": (note,)}}).files == {"notes": [FileSpec(content="x", filename="n.txt")]}
        with pytest.raises(ValidationError, match="got set"):
            Multipart.model_validate({"fields": {"tags": {"a", "b"}}})
