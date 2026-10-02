"""Unit tests for all RequestBody types."""

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


def test_json_namespace_becomes_dict():
    """A rendered ``{{ var }}`` may be a SimpleNamespace (vars are namespaces);
    JSON-serialized fields turn it back into a plain dict, recursively."""
    body = JsonBody(json=SimpleNamespace(a=1, b=[SimpleNamespace(c=2)], d={"e": SimpleNamespace(f=3)}))
    assert body.json == {"a": 1, "b": [{"c": 2}], "d": {"e": {"f": 3}}}


def test_graphql_variables_namespace_becomes_dict():
    assert GraphQL(query="{ a }", variables=SimpleNamespace(id="1")).variables == {"id": "1"}


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
