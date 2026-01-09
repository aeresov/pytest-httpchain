"""Unit tests for all RequestBody types."""

import base64
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
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


class TestTextBody:
    """Tests for TextBody model."""

    def test_text_body_with_string(self):
        """Test TextBody with plain string."""
        body = TextBody(text="Hello, World!")
        assert body.text == "Hello, World!"

    def test_text_body_with_template(self):
        """Test TextBody with template expression."""
        body = TextBody(text="{{ message }}")
        assert body.text == "{{ message }}"

    def test_text_body_with_partial_template(self):
        """Test TextBody with partial template."""
        body = TextBody(text="prefix {{ value }} suffix")
        assert body.text == "prefix {{ value }} suffix"

    def test_text_body_in_request(self):
        """Test TextBody as part of Request model."""
        request = Request(url="https://example.com/api", body=TextBody(text="raw content"))
        assert isinstance(request.body, TextBody)
        assert request.body.text == "raw content"

    def test_text_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            TextBody(text="content", extra="field")  # type: ignore[call-arg]


class TestBase64Body:
    """Tests for Base64Body model."""

    def test_base64_body_with_valid_base64(self):
        """Test Base64Body with valid base64 string."""
        # "Hello, World!" in base64
        encoded = base64.b64encode(b"Hello, World!").decode()
        body = Base64Body(base64=encoded)
        assert body.base64 == encoded

    def test_base64_body_with_invalid_base64(self):
        """Test Base64Body rejects invalid base64."""
        with pytest.raises(ValidationError, match="Invalid base64 encoding"):
            Base64Body(base64="not-valid-base64!!!")

    def test_base64_body_with_incorrect_padding(self):
        """Test Base64Body rejects base64 with incorrect padding."""
        with pytest.raises(ValidationError, match="Invalid base64 encoding"):
            Base64Body(base64="SGVsbG8")  # Missing padding

    def test_base64_body_with_template(self):
        """Test Base64Body with template expression (bypasses validation)."""
        body = Base64Body(base64="{{ encoded_data }}")
        assert body.base64 == "{{ encoded_data }}"

    def test_base64_body_with_partial_template(self):
        """Test Base64Body with partial template (bypasses validation)."""
        body = Base64Body(base64="prefix{{ value }}")
        assert body.base64 == "prefix{{ value }}"

    def test_base64_body_in_request(self):
        """Test Base64Body as part of Request model."""
        encoded = base64.b64encode(b"test data").decode()
        request = Request(url="https://example.com/api", body=Base64Body(base64=encoded))
        assert isinstance(request.body, Base64Body)
        assert request.body.base64 == encoded

    def test_base64_body_empty_string(self):
        """Test Base64Body with empty string (valid base64)."""
        body = Base64Body(base64="")
        assert body.base64 == ""

    def test_base64_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        encoded = base64.b64encode(b"test").decode()
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            Base64Body(base64=encoded, extra="field")  # type: ignore[call-arg]


class TestBinaryBody:
    """Tests for BinaryBody model."""

    def test_binary_body_with_path_string(self):
        """Test BinaryBody with path as string."""
        body = BinaryBody(binary="csvs/mydata.csv")
        assert isinstance(body.binary, Path)
        assert str(body.binary) == "csvs/mydata.csv"

    def test_binary_body_with_path_object(self):
        """Test BinaryBody with Path object."""
        path = Path("data/file.bin")
        body = BinaryBody(binary=path)
        assert body.binary == path

    def test_binary_body_with_template(self):
        """Test BinaryBody with template expression."""
        body = BinaryBody(binary="{{ file_path }}")
        assert body.binary == "{{ file_path }}"

    def test_binary_body_with_partial_template(self):
        """Test BinaryBody with partial template."""
        body = BinaryBody(binary="data/{{ filename }}.csv")
        assert body.binary == "data/{{ filename }}.csv"

    def test_binary_body_in_request(self):
        """Test BinaryBody as part of Request model."""
        request = Request(url="https://example.com/upload", body=BinaryBody(binary="files/data.bin"))
        assert isinstance(request.body, BinaryBody)
        assert isinstance(request.body.binary, Path)
        assert str(request.body.binary) == "files/data.bin"

    def test_binary_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BinaryBody(binary="file.bin", extra="field")  # type: ignore[call-arg]


class TestBodyTypeDiscriminator:
    """Tests for body type discrimination using raw dicts."""

    def test_discriminator_chooses_text_body(self):
        """Test discriminator correctly identifies TextBody."""
        request = Request(url="https://example.com", body={"text": "content"})  # type: ignore[arg-type]
        assert isinstance(request.body, TextBody)

    def test_discriminator_chooses_base64_body(self):
        """Test discriminator correctly identifies Base64Body."""
        encoded = base64.b64encode(b"test").decode()
        request = Request(url="https://example.com", body={"base64": encoded})  # type: ignore[arg-type]
        assert isinstance(request.body, Base64Body)

    def test_discriminator_chooses_binary_body(self):
        """Test discriminator correctly identifies BinaryBody."""
        request = Request(url="https://example.com", body={"binary": "file.bin"})  # type: ignore[arg-type]
        assert isinstance(request.body, BinaryBody)

    def test_multiple_body_types_not_allowed(self):
        """Test that only one body type can be specified."""
        with pytest.raises(ValidationError):
            Request(url="https://example.com", body={"text": "content", "base64": "encoded"})  # type: ignore[arg-type]


class TestJsonBody:
    """Tests for JsonBody model."""

    def test_json_body_with_dict(self):
        """Test JsonBody with dictionary."""
        body = JsonBody(json={"key": "value", "number": 42})
        assert body.json == {"key": "value", "number": 42}

    def test_json_body_with_list(self):
        """Test JsonBody with list."""
        body = JsonBody(json=[1, 2, 3])
        assert body.json == [1, 2, 3]

    def test_json_body_with_nested_structure(self):
        """Test JsonBody with nested JSON."""
        data = {"users": [{"name": "Alice"}, {"name": "Bob"}], "count": 2}
        body = JsonBody(json=data)
        assert body.json == data

    def test_json_body_with_primitives(self):
        """Test JsonBody with primitive values."""
        assert JsonBody(json="string").json == "string"
        assert JsonBody(json=42).json == 42
        assert JsonBody(json=3.14).json == 3.14
        assert JsonBody(json=True).json is True
        assert JsonBody(json=None).json is None

    def test_json_body_in_request(self):
        """Test JsonBody as part of Request model."""
        request = Request(url="https://example.com/api", body=JsonBody(json={"data": "test"}))
        assert isinstance(request.body, JsonBody)
        assert request.body.json == {"data": "test"}

    def test_json_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            JsonBody(json={"key": "value"}, extra="field")  # type: ignore[call-arg]


class TestXmlBody:
    """Tests for XmlBody model."""

    def test_xml_body_with_simple_xml(self):
        """Test XmlBody with simple XML."""
        xml = "<root>content</root>"
        body = XmlBody(xml=xml)
        assert body.xml == xml

    def test_xml_body_with_nested_xml(self):
        """Test XmlBody with nested XML."""
        xml = "<root><child><value>test</value></child></root>"
        body = XmlBody(xml=xml)
        assert body.xml == xml

    def test_xml_body_with_attributes(self):
        """Test XmlBody with attributes."""
        xml = '<user id="1" active="true">John</user>'
        body = XmlBody(xml=xml)
        assert body.xml == xml

    def test_xml_body_with_template(self):
        """Test XmlBody with template expression."""
        body = XmlBody(xml="<root>{{ content }}</root>")
        assert body.xml == "<root>{{ content }}</root>"

    def test_xml_body_invalid_xml_rejected(self):
        """Test that invalid XML is rejected."""
        with pytest.raises(ValidationError, match="Invalid XML"):
            XmlBody(xml="<root>unclosed")

    def test_xml_body_in_request(self):
        """Test XmlBody as part of Request model."""
        xml = "<data><value>test</value></data>"
        request = Request(url="https://example.com/api", body=XmlBody(xml=xml))
        assert isinstance(request.body, XmlBody)
        assert request.body.xml == xml

    def test_xml_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            XmlBody(xml="<root/>", extra="field")  # type: ignore[call-arg]


class TestFormBody:
    """Tests for FormBody model."""

    def test_form_body_simple(self):
        """Test FormBody with simple form data."""
        body = FormBody(form={"username": "john", "password": "secret"})
        assert body.form == {"username": "john", "password": "secret"}

    def test_form_body_with_various_types(self):
        """Test FormBody with various value types."""
        body = FormBody(form={"string": "value", "number": 42, "boolean": True})
        assert body.form["string"] == "value"
        assert body.form["number"] == 42
        assert body.form["boolean"] is True

    def test_form_body_empty(self):
        """Test FormBody with empty form."""
        body = FormBody(form={})
        assert body.form == {}

    def test_form_body_in_request(self):
        """Test FormBody as part of Request model."""
        request = Request(url="https://example.com/api", body=FormBody(form={"field": "value"}))
        assert isinstance(request.body, FormBody)
        assert request.body.form == {"field": "value"}

    def test_form_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            FormBody(form={"key": "value"}, extra="field")  # type: ignore[call-arg]


class TestFilesBody:
    """Tests for FilesBody model."""

    def test_files_body_single_file(self):
        """Test FilesBody with single file."""
        body = FilesBody(files={"document": "path/to/file.pdf"})
        assert isinstance(body.files["document"], Path)
        assert str(body.files["document"]) == "path/to/file.pdf"

    def test_files_body_multiple_files(self):
        """Test FilesBody with multiple files."""
        body = FilesBody(files={"doc1": "file1.pdf", "doc2": "file2.pdf"})
        assert len(body.files) == 2
        assert str(body.files["doc1"]) == "file1.pdf"
        assert str(body.files["doc2"]) == "file2.pdf"

    def test_files_body_with_path_object(self):
        """Test FilesBody with Path object."""
        body = FilesBody(files={"file": Path("data/upload.bin")})
        assert body.files["file"] == Path("data/upload.bin")

    def test_files_body_with_template(self):
        """Test FilesBody with template expression."""
        body = FilesBody(files={"file": "uploads/{{ filename }}"})
        assert body.files["file"] == "uploads/{{ filename }}"

    def test_files_body_in_request(self):
        """Test FilesBody as part of Request model."""
        request = Request(url="https://example.com/upload", body=FilesBody(files={"upload": "file.pdf"}))
        assert isinstance(request.body, FilesBody)

    def test_files_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            FilesBody(files={"f": "file.txt"}, extra="field")  # type: ignore[call-arg]


class TestGraphQLBody:
    """Tests for GraphQLBody model."""

    def test_graphql_body_simple_query(self):
        """Test GraphQLBody with simple query."""
        body = GraphQLBody(graphql=GraphQL(query="{ user { id name } }"))
        assert isinstance(body.graphql, GraphQL)
        assert body.graphql.query == "{ user { id name } }"

    def test_graphql_body_with_variables(self):
        """Test GraphQLBody with query and variables."""
        body = GraphQLBody(
            graphql=GraphQL(
                query="query GetUser($id: ID!) { user(id: $id) { name } }",
                variables={"id": "123"},
            )
        )
        assert body.graphql.query == "query GetUser($id: ID!) { user(id: $id) { name } }"
        assert body.graphql.variables == {"id": "123"}

    def test_graphql_body_mutation(self):
        """Test GraphQLBody with mutation."""
        body = GraphQLBody(
            graphql=GraphQL(
                query="mutation CreateUser($name: String!) { createUser(name: $name) { id } }",
                variables={"name": "Alice"},
            )
        )
        assert "mutation" in body.graphql.query

    def test_graphql_body_mutation_from_file(self, datadir):
        """Test GraphQLBody with mutation loaded from file."""
        query = (datadir / "mutation_create_user.graphql").read_text()
        body = GraphQLBody(
            graphql=GraphQL(
                query=query,
                variables={"input": {"name": "Alice", "email": "alice@example.com"}},
            )
        )
        assert "mutation CreateUser" in body.graphql.query
        assert "createUser(input: $input)" in body.graphql.query

    def test_graphql_body_query_with_directives(self, datadir):
        """Test GraphQLBody with query containing directives loaded from file."""
        query = (datadir / "query_with_variables.graphql").read_text()
        body = GraphQLBody(
            graphql=GraphQL(
                query=query,
                variables={"id": "123", "includeProfile": True},
            )
        )
        assert "@include(if: $includeProfile)" in body.graphql.query

    def test_graphql_body_with_template_query(self):
        """Test GraphQLBody with template in query."""
        body = GraphQLBody(graphql=GraphQL(query="{{ graphql_query }}"))
        assert body.graphql.query == "{{ graphql_query }}"

    def test_graphql_body_invalid_query_rejected(self):
        """Test that invalid GraphQL query is rejected."""
        with pytest.raises(ValidationError, match="Invalid GraphQL query"):
            GraphQLBody(graphql=GraphQL(query="{ user { id name }"))  # Missing closing brace

    def test_graphql_body_in_request(self):
        """Test GraphQLBody as part of Request model."""
        request = Request(
            url="https://example.com/graphql",
            body=GraphQLBody(graphql=GraphQL(query="{ users { id } }")),
        )
        assert isinstance(request.body, GraphQLBody)

    def test_graphql_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed on GraphQL."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            GraphQLBody(graphql={"query": "{ test }", "extra": "field"})  # type: ignore[arg-type]

    def test_graphql_body_outer_extra_fields_forbidden(self):
        """Test that extra fields are not allowed on GraphQLBody."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            GraphQLBody(graphql=GraphQL(query="{ test }"), extra="field")  # type: ignore[call-arg]


class TestBodyTypeDiscriminatorExtended:
    """Extended tests for body type discrimination using raw dicts."""

    def test_discriminator_chooses_json_body(self):
        """Test discriminator correctly identifies JsonBody."""
        request = Request(url="https://example.com", body={"json": {"data": "test"}})  # type: ignore[arg-type]
        assert isinstance(request.body, JsonBody)

    def test_discriminator_chooses_xml_body(self):
        """Test discriminator correctly identifies XmlBody."""
        request = Request(url="https://example.com", body={"xml": "<root/>"})  # type: ignore[arg-type]
        assert isinstance(request.body, XmlBody)

    def test_discriminator_chooses_form_body(self):
        """Test discriminator correctly identifies FormBody."""
        request = Request(url="https://example.com", body={"form": {"key": "value"}})  # type: ignore[arg-type]
        assert isinstance(request.body, FormBody)

    def test_discriminator_chooses_files_body(self):
        """Test discriminator correctly identifies FilesBody."""
        request = Request(url="https://example.com", body={"files": {"f": "file.txt"}})  # type: ignore[arg-type]
        assert isinstance(request.body, FilesBody)

    def test_discriminator_chooses_graphql_body(self):
        """Test discriminator correctly identifies GraphQLBody."""
        request = Request(url="https://example.com", body={"graphql": {"query": "{ test }"}})  # type: ignore[arg-type]
        assert isinstance(request.body, GraphQLBody)
