"""Unit tests for TextBody, Base64Body, and BinaryBody models."""

import base64
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    Base64Body,
    BinaryBody,
    Request,
    TextBody,
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
        request = Request(url="https://example.com/api", body={"text": "raw content"})
        assert isinstance(request.body, TextBody)
        assert request.body.text == "raw content"

    def test_text_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            TextBody(text="content", extra="field")


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
        request = Request(url="https://example.com/api", body={"base64": encoded})
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
            Base64Body(base64=encoded, extra="field")


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
        request = Request(url="https://example.com/upload", body={"binary": "files/data.bin"})
        assert isinstance(request.body, BinaryBody)
        assert isinstance(request.body.binary, Path)
        assert str(request.body.binary) == "files/data.bin"

    def test_binary_body_extra_fields_forbidden(self):
        """Test that extra fields are not allowed."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            BinaryBody(binary="file.bin", extra="field")


class TestBodyTypeDiscriminator:
    """Tests for body type discrimination."""

    def test_discriminator_chooses_text_body(self):
        """Test discriminator correctly identifies TextBody."""
        request = Request(url="https://example.com", body={"text": "content"})
        assert isinstance(request.body, TextBody)

    def test_discriminator_chooses_base64_body(self):
        """Test discriminator correctly identifies Base64Body."""
        encoded = base64.b64encode(b"test").decode()
        request = Request(url="https://example.com", body={"base64": encoded})
        assert isinstance(request.body, Base64Body)

    def test_discriminator_chooses_binary_body(self):
        """Test discriminator correctly identifies BinaryBody."""
        request = Request(url="https://example.com", body={"binary": "file.bin"})
        assert isinstance(request.body, BinaryBody)

    def test_multiple_body_types_not_allowed(self):
        """Test that only one body type can be specified."""
        with pytest.raises(ValidationError):
            Request(url="https://example.com", body={"text": "content", "base64": "encoded"})
