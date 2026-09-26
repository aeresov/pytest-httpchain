"""Unit tests for all RequestBody types."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    Base64Body,
    BinaryBody,
    FilesBody,
    FormBody,
    GraphQL,
    GraphQLBody,
    JsonBody,
    Request,
    TextBody,
    XmlBody,
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
        pytest.param({"files": {"f": "file.txt"}}, FilesBody, id="files"),
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
