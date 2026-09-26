"""Unit tests for Verify and ResponseBody models."""

import json
from http import HTTPStatus
from pathlib import Path

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    HeaderMatcher,
    ResponseBody,
    UserFunctionKwargs,
    UserFunctionName,
    Verify,
)
from tests.unit.models.helpers import assert_error_types


class TestVerifyFields:
    @pytest.mark.parametrize(
        ("attr", "default"),
        [
            ("status", None),
            ("headers", {}),
            ("expressions", []),
            ("user_functions", []),
            ("description", None),
            ("body", ResponseBody()),
        ],
    )
    def test_field_default(self, attr, default):
        assert getattr(Verify(), attr) == default

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("expressions", ["{{ status_code == 200 }}", "{{ 'error' not in response_text }}", "{{ user_age >= 18 }}"], id="expressions"),
            pytest.param("description", "Verify successful user creation", id="description"),
            pytest.param(
                "user_functions",
                [UserFunctionName("validators:check_schema"), UserFunctionKwargs(name=UserFunctionName("validators:custom"), kwargs={"strict": True})],
                id="user_functions",
            ),
            pytest.param("body", ResponseBody(schema={"type": "object"}, contains=["success"]), id="body"),
        ],
    )
    def test_field_round_trip(self, field, value):
        assert getattr(Verify(**{field: value}), field) == value


class TestVerifyStatus:
    @pytest.mark.parametrize(
        "status",
        [
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
            pytest.param(200, id="int-200"),
            # Any int in 100-599 is assertable (nginx 499, vendor codes).
            418,
            425,
            499,
            599,
            "{{ expected_status }}",
        ],
    )
    def test_accepted(self, status):
        assert Verify(status=status).status == status

    @pytest.mark.parametrize(
        ("status", "error_type"),
        [(99, "greater_than_equal"), (0, "greater_than_equal"), (-200, "greater_than_equal"), (600, "less_than_equal")],
    )
    def test_outside_http_range_rejected(self, status, error_type):
        with pytest.raises(ValidationError) as exc_info:
            Verify(status=status)
        assert_error_types(exc_info, error_type, at="status")


class TestVerifyHeaders:
    """A header value is an exact string or a matcher object."""

    def test_strings_and_matchers(self):
        headers = {"Content-Type": "application/json", "Cache-Control": "no-cache", "x-type": {"contains": "json"}, "x-id": {"matches": "^[0-9]+$"}}
        assert Verify(headers=headers).headers == {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "x-type": HeaderMatcher(contains="json"),
            "x-id": HeaderMatcher(matches="^[0-9]+$"),
        }

    def test_empty_matcher_rejected(self):
        with pytest.raises(ValidationError, match="at least one"):
            Verify(headers={"content-type": {}})


class TestResponseBody:
    def test_defaults(self):
        assert ResponseBody().model_dump() == {"schema": None, "contains": [], "not_contains": [], "matches": [], "not_matches": []}

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("contains", ["success", "user_id", "created"]),
            ("not_contains", ["error", "failed", "unauthorized"]),
            ("matches", [r"\d{4}-\d{2}-\d{2}", r"user_\d+"]),
            ("not_matches", [r"error:\s*", r"exception"]),
        ],
    )
    def test_list_field_round_trip(self, field, value):
        assert getattr(ResponseBody(**{field: value}), field) == value

    @pytest.mark.parametrize(
        ("schema", "expected"),
        [
            pytest.param({"type": "object", "required": ["id"]}, {"type": "object", "required": ["id"]}, id="inline"),
            pytest.param("schemas/user.json", Path("schemas/user.json"), id="path"),
            pytest.param("{{ schema_path }}", "{{ schema_path }}", id="template"),
        ],
    )
    def test_schema_forms(self, schema, expected):
        assert ResponseBody(schema=schema).schema == expected

    def test_schema_from_file(self, datadir):
        schema = json.loads((datadir / "user_response_schema.json").read_text())
        assert ResponseBody(schema=schema).schema == schema

    @pytest.mark.parametrize(
        ("construct", "message"),
        [
            pytest.param(lambda: ResponseBody(matches=["[invalid"]), "Invalid regular expression", id="regex"),
            pytest.param(lambda: ResponseBody(schema={"type": "not_a_type"}), "Invalid JSON Schema", id="schema"),
        ],
    )
    def test_invalid_content_rejected(self, construct, message):
        """Wiring only: the exhaustive cases live in test_type_validators.py."""
        with pytest.raises(ValidationError, match=message):
            construct()
