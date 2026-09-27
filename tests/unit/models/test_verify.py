"""Unit tests for Verify and ResponseBody models."""

import json
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

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

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            pytest.param("2xx", "2xx", id="class"),
            *(pytest.param(f"{digit}xx", f"{digit}xx", id=f"class-{digit}xx") for digit in (1, 3, 4, 5)),
            # Either case, kept lowercase: one spelling for the check and its message.
            pytest.param("2XX", "2xx", id="class-uppercase"),
            pytest.param("4xX", "4xx", id="class-mixed-case"),
            pytest.param([200, 201], [200, 201], id="list-of-codes"),
            pytest.param(["2xx", 304], ["2xx", 304], id="list-mixed"),
            pytest.param(["3XX"], ["3xx"], id="list-of-one-class"),
            # An entry takes a template of its own, rendered to a code or a class.
            pytest.param(["{{ expected }}", 304], ["{{ expected }}", 304], id="list-with-template"),
            # A rendered tuple is the list it stands for.
            pytest.param((200, 201), [200, 201], id="tuple"),
        ],
    )
    def test_class_and_list_forms(self, status, expected):
        assert Verify(status=status).status == expected

    def test_list_entries_are_codes_as_a_single_status_is(self):
        """Each entry is validated as a single status is: a standard code, a
        stringified one, a nonstandard one."""
        assert Verify(status=[HTTPStatus.OK, "201", 499]).status == [200, 201, 499]

    @pytest.mark.parametrize(
        ("status", "error_type"),
        [
            # Only the five classes HTTP defines.
            pytest.param("6xx", "string_pattern_mismatch", id="class-6xx"),
            pytest.param("0xx", "string_pattern_mismatch", id="class-0xx"),
            pytest.param("2x", "string_pattern_mismatch", id="class-short"),
            pytest.param("2xxx", "string_pattern_mismatch", id="class-long"),
            pytest.param("20x", "string_pattern_mismatch", id="class-partial"),
            pytest.param(" 2xx", "string_pattern_mismatch", id="class-padded"),
            # An empty list would match nothing, or everything.
            pytest.param([], "too_short", id="empty-list"),
            pytest.param([200, 600], "less_than_equal", id="list-entry-out-of-range"),
            pytest.param([200, "6xx"], "string_pattern_mismatch", id="list-entry-bad-class"),
            pytest.param([[200, 201]], "int_type", id="nested-list"),
            pytest.param([None], "int_type", id="list-entry-null"),
        ],
    )
    def test_invalid_class_or_list_rejected(self, status, error_type):
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

    def test_namespace_is_the_matcher_object(self):
        """A matcher written as one template over ``vars`` (``"{{ ct }}"``)
        renders as a SimpleNamespace, which walk()'s re-validation refused as
        neither a string nor a matcher object. It is the matcher it was
        declared as, and checked as one: an unknown key is still refused."""
        assert Verify(headers={"x-type": SimpleNamespace(contains="json")}).headers == {"x-type": HeaderMatcher(contains="json")}
        with pytest.raises(ValidationError) as exc_info:
            Verify(headers={"x-type": SimpleNamespace(contain="json")})
        assert_error_types(exc_info, "extra_forbidden", at="contain")


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

    def test_schema_from_namespace(self):
        """A schema in ``vars`` renders as namespaces all the way down, which
        walk()'s re-validation refused as not a dict. A schema is plain JSON, so
        every level converts, lists included."""
        schema = SimpleNamespace(type="object", properties=SimpleNamespace(id=SimpleNamespace(type="integer")), allOf=[SimpleNamespace(required=["id"])])
        assert ResponseBody(schema=schema).schema == {"type": "object", "properties": {"id": {"type": "integer"}}, "allOf": [{"required": ["id"]}]}

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
