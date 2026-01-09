"""Unit tests for carrier.py - error cases and edge cases only.

Success cases for body types, verify, and save are covered by integration tests:
- tests/integration/test_body_types.py
- tests/integration/test_verify.py
- tests/integration/test_save.py
- tests/integration/test_errors.py
"""

import json
import tempfile
from collections import ChainMap
from http import HTTPMethod
from pathlib import Path

import httpx
import pytest
from pytest_httpchain_models import (
    BinaryBody,
    FilesBody,
    JMESPathSave,
    Request,
    Verify,
)
from pytest_httpchain_models.entities import ResponseBody

from pytest_httpchain.carrier import Carrier
from pytest_httpchain.exceptions import RequestError, SaveError, VerificationError


class TestBuildRequestKwargsErrors:
    """Error cases not covered by integration tests."""

    def test_binary_body_file_not_found(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=BinaryBody(binary="/nonexistent/file.bin"),
        )

        with pytest.raises(RequestError, match="Binary file not found"):
            Carrier._build_request_kwargs(request)

    def test_files_body_file_not_found(self):
        request = Request(
            url="https://example.com/api",
            method=HTTPMethod.POST,
            body=FilesBody(files={"upload": "/nonexistent/file.txt"}),
        )

        with pytest.raises(RequestError, match="File not found for upload"):
            Carrier._build_request_kwargs(request)


class TestProcessSaveStepErrors:
    """Error cases not covered by integration tests."""

    def test_jmespath_save_invalid_json_response(self):
        response = httpx.Response(
            200,
            content=b"not valid json",
            headers={"content-type": "text/plain"},
        )
        save_model = JMESPathSave(jmespath={"value": "key"})
        context = ChainMap()

        with pytest.raises(SaveError, match="response is not valid JSON"):
            Carrier._process_save_step(save_model, response, context)


class TestProcessVerifyStepErrors:
    """Error cases and edge cases not covered by integration tests."""

    def test_verify_body_schema_file_not_found(self):
        response = httpx.Response(200, json={"id": 123})
        verify = Verify(body=ResponseBody(schema="/nonexistent/schema.json"))

        with pytest.raises(VerificationError, match="Error reading body schema file"):
            Carrier._process_verify_step(verify, response)

    def test_verify_body_schema_invalid_json_response(self):
        response = httpx.Response(
            200,
            content=b"not json",
            headers={"content-type": "text/plain"},
        )
        schema = {"type": "object"}
        verify = Verify(body=ResponseBody(schema=schema))

        with pytest.raises(VerificationError, match="response is not valid JSON"):
            Carrier._process_verify_step(verify, response)

    def test_verify_body_schema_from_file(self):
        """Test schema loaded from file path - unique to unit tests."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w") as f:
            json.dump(
                {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}},
                    "required": ["id"],
                },
                f,
            )
            schema_path = f.name

        try:
            response = httpx.Response(200, json={"id": 123})
            verify = Verify(body=ResponseBody(schema=schema_path))

            # Should not raise
            Carrier._process_verify_step(verify, response)
        finally:
            Path(schema_path).unlink()

    def test_verify_expressions_falsy_values(self):
        """Test that falsy expression values fail verification."""
        response = httpx.Response(200)
        verify = Verify(expressions=[True, False, True])

        with pytest.raises(VerificationError, match="Expression.*failed"):
            Carrier._process_verify_step(verify, response)

    def test_verify_expressions_empty_string_fails(self):
        """Test that empty string expression fails."""
        response = httpx.Response(200)
        verify = Verify(expressions=[""])

        with pytest.raises(VerificationError, match="Expression.*failed"):
            Carrier._process_verify_step(verify, response)
