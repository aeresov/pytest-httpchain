"""Unit tests for Verify and ResponseBody models."""

import json
from http import HTTPMethod, HTTPStatus

import pytest
from pydantic import ValidationError
from pytest_httpchain_models.entities import (
    Request,
    ResponseBody,
    Stage,
    UserFunctionKwargs,
    UserFunctionName,
    Verify,
    VerifyStep,
)


class TestVerifyStatus:
    """Tests for Verify.status field."""

    def test_status_default_none(self):
        """Test default status is None."""
        verify = Verify()
        assert verify.status is None

    def test_status_integer(self):
        """Test status with integer value."""
        verify = Verify(status=HTTPStatus.OK)
        assert verify.status == HTTPStatus.OK

    def test_status_http_status(self):
        """Test status with HTTPStatus enum."""
        verify = Verify(status=HTTPStatus.CREATED)
        assert verify.status == HTTPStatus.CREATED

    def test_status_various_codes(self):
        """Test various HTTP status codes."""
        codes = [
            HTTPStatus.OK,
            HTTPStatus.CREATED,
            HTTPStatus.NO_CONTENT,
            HTTPStatus.BAD_REQUEST,
            HTTPStatus.UNAUTHORIZED,
            HTTPStatus.FORBIDDEN,
            HTTPStatus.NOT_FOUND,
            HTTPStatus.INTERNAL_SERVER_ERROR,
            HTTPStatus.BAD_GATEWAY,
            HTTPStatus.SERVICE_UNAVAILABLE,
        ]
        for code in codes:
            verify = Verify(status=code)
            assert verify.status == code

    def test_status_with_template(self):
        """Test status with template expression."""
        verify = Verify(status="{{ expected_status }}")
        assert verify.status == "{{ expected_status }}"


class TestVerifyHeaders:
    """Tests for Verify.headers field."""

    def test_headers_default_empty(self):
        """Test default headers is empty dict."""
        verify = Verify()
        assert verify.headers == {}

    def test_headers_content_type(self):
        """Test headers with content type."""
        verify = Verify(headers={"Content-Type": "application/json"})
        assert verify.headers["Content-Type"] == "application/json"

    def test_headers_multiple(self):
        """Test multiple headers."""
        verify = Verify(
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "X-Request-Id": "abc123",
            }
        )
        assert len(verify.headers) == 3


class TestVerifyExpressions:
    """Tests for Verify.expressions field."""

    def test_expressions_default_empty(self):
        """Test default expressions is empty list."""
        verify = Verify()
        assert verify.expressions == []

    def test_expressions_simple(self):
        """Test simple template expressions."""
        verify = Verify(
            expressions=[
                "{{ status_code == 200 }}",
                "{{ 'error' not in response_text }}",
            ]
        )
        assert len(verify.expressions) == 2

    def test_expressions_comparison(self):
        """Test comparison expressions."""
        verify = Verify(
            expressions=[
                "{{ user_age >= 18 }}",
                "{{ item_count > 0 }}",
                "{{ balance <= limit }}",
            ]
        )
        assert len(verify.expressions) == 3


class TestVerifyUserFunctions:
    """Tests for Verify.user_functions field."""

    def test_user_functions_default_empty(self):
        """Test default user_functions is empty list."""
        verify = Verify()
        assert verify.user_functions == []

    def test_user_functions_simple(self):
        """Test simple function names."""
        verify = Verify(user_functions=[UserFunctionName("validators:check_response")])
        assert len(verify.user_functions) == 1

    def test_user_functions_multiple(self):
        """Test multiple user functions."""
        verify = Verify(
            user_functions=[
                UserFunctionName("validators:check_schema"),
                UserFunctionName("validators:check_permissions"),
            ]
        )
        assert len(verify.user_functions) == 2

    def test_user_functions_with_kwargs(self):
        """Test user functions with kwargs."""
        verify = Verify(
            user_functions=[
                UserFunctionKwargs(
                    name=UserFunctionName("validators:custom"),
                    kwargs={"strict": True},
                )
            ]
        )
        assert len(verify.user_functions) == 1


class TestVerifyDescription:
    """Tests for Verify.description field."""

    def test_description_default_none(self):
        """Test default description is None."""
        verify = Verify()
        assert verify.description is None

    def test_description_custom(self):
        """Test custom description."""
        verify = Verify(
            status=HTTPStatus.OK,
            description="Verify successful user creation",
        )
        assert verify.description == "Verify successful user creation"


class TestResponseBody:
    """Tests for ResponseBody model."""

    def test_response_body_defaults(self):
        """Test ResponseBody default values."""
        body = ResponseBody()
        assert body.schema is None
        assert body.contains == []
        assert body.not_contains == []
        assert body.matches == []
        assert body.not_matches == []

    def test_response_body_schema_inline(self):
        """Test ResponseBody with inline JSON schema."""
        body = ResponseBody(
            schema={
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
            }
        )
        assert isinstance(body.schema, dict)
        assert body.schema["type"] == "object"

    def test_response_body_schema_path(self):
        """Test ResponseBody with schema file path."""
        from pathlib import Path

        body = ResponseBody(schema="schemas/user.json")
        assert isinstance(body.schema, Path)

    def test_response_body_schema_template(self):
        """Test ResponseBody with schema template."""
        body = ResponseBody(schema="{{ schema_path }}")
        assert body.schema == "{{ schema_path }}"

    def test_response_body_contains(self):
        """Test ResponseBody with contains assertions."""
        body = ResponseBody(contains=["success", "user_id", "created"])
        assert len(body.contains) == 3

    def test_response_body_not_contains(self):
        """Test ResponseBody with not_contains assertions."""
        body = ResponseBody(not_contains=["error", "failed", "unauthorized"])
        assert len(body.not_contains) == 3

    def test_response_body_matches(self):
        """Test ResponseBody with regex matches."""
        body = ResponseBody(matches=[r"\d{4}-\d{2}-\d{2}", r"user_\d+"])
        assert len(body.matches) == 2

    def test_response_body_not_matches(self):
        """Test ResponseBody with regex not_matches."""
        body = ResponseBody(not_matches=[r"error:\s*", r"exception"])
        assert len(body.not_matches) == 2

    def test_response_body_invalid_regex_rejected(self):
        """Test that invalid regex patterns are rejected."""
        with pytest.raises(ValidationError, match="Invalid regular expression"):
            ResponseBody(matches=["[invalid"])

    def test_response_body_invalid_schema_rejected(self):
        """Test that invalid JSON schema is rejected."""
        with pytest.raises(ValidationError, match="Invalid JSON Schema"):
            ResponseBody(schema={"type": "not_a_type"})

    def test_response_body_schema_from_file(self, datadir):
        """Test ResponseBody with complex schema loaded from file."""
        schema = json.loads((datadir / "user_response_schema.json").read_text())
        body = ResponseBody(schema=schema)
        assert isinstance(body.schema, dict)
        assert body.schema["type"] == "object"
        assert "id" in body.schema["properties"]
        assert "status" in body.schema["properties"]
        assert body.schema["properties"]["status"]["enum"] == ["active", "inactive", "pending"]


class TestVerifyBody:
    """Tests for Verify.body field (ResponseBody)."""

    def test_verify_body_default(self):
        """Test default body is empty ResponseBody."""
        verify = Verify()
        assert isinstance(verify.body, ResponseBody)

    def test_verify_body_with_schema(self):
        """Test verify with body schema."""
        verify = Verify(body=ResponseBody(schema={"type": "object"}))
        assert verify.body.schema == {"type": "object"}

    def test_verify_body_with_contains(self):
        """Test verify with body contains."""
        verify = Verify(body=ResponseBody(contains=["success"]))
        assert "success" in verify.body.contains


class TestVerifyStep:
    """Tests for VerifyStep wrapper model."""

    def test_verify_step_simple(self):
        """Test simple VerifyStep."""
        step = VerifyStep(verify=Verify(status=HTTPStatus.OK))
        assert isinstance(step.verify, Verify)
        assert step.verify.status == HTTPStatus.OK

    def test_verify_step_full(self):
        """Test VerifyStep with full configuration."""
        step = VerifyStep(
            verify=Verify(
                status=HTTPStatus.CREATED,
                headers={"Content-Type": "application/json"},
                expressions=["{{ response_json.success == true }}"],
                body=ResponseBody(
                    contains=["created"],
                    schema={"type": "object"},
                ),
            )
        )
        assert step.verify.status == HTTPStatus.CREATED
        assert len(step.verify.headers) == 1


class TestVerifyInStage:
    """Tests for Verify in Stage response."""

    def test_stage_with_verify_status(self):
        """Test Stage with status verification."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[VerifyStep(verify=Verify(status=HTTPStatus.OK))],
        )
        assert len(stage.response) == 1
        assert isinstance(stage.response[0], VerifyStep)

    def test_stage_with_multiple_verifications(self):
        """Test Stage with multiple verify steps."""
        stage = Stage(
            name="test",
            request=Request(url="https://example.com"),
            response=[
                VerifyStep(verify=Verify(status=HTTPStatus.OK)),
                VerifyStep(verify=Verify(headers={"Content-Type": "application/json"})),
                VerifyStep(verify=Verify(body=ResponseBody(contains=["success"]))),
            ],
        )
        assert len(stage.response) == 3
        assert all(isinstance(s, VerifyStep) for s in stage.response)

    def test_stage_with_complex_verification(self):
        """Test Stage with complex verification."""
        stage = Stage(
            name="create-user",
            request=Request(url="https://example.com/users", method=HTTPMethod.POST),
            response=[
                VerifyStep(
                    verify=Verify(
                        status=HTTPStatus.CREATED,
                        headers={"Content-Type": "application/json"},
                        expressions=[
                            "{{ response_json.id is defined }}",
                            "{{ response_json.name == request_json.name }}",
                        ],
                        body=ResponseBody(
                            schema={
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "name": {"type": "string"},
                                },
                                "required": ["id", "name"],
                            }
                        ),
                        description="Verify user was created successfully",
                    )
                )
            ],
        )
        verify = stage.response[0].verify
        assert verify.status == HTTPStatus.CREATED
        assert verify.description == "Verify user was created successfully"
