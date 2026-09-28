"""Unit tests for Verify and ResponseBody models."""

import json
import re
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pytest_httpchain.models.entities import (
    HeaderMatcher,
    JMESPathMatcher,
    ResponseBody,
    UserFunctionKwargs,
    UserFunctionName,
    Verify,
    validate_rendered_verify,
)
from tests.unit.models.helpers import assert_error_types


class TestVerifyFields:
    @pytest.mark.parametrize(
        ("attr", "default"),
        [
            ("status", None),
            ("headers", {}),
            ("jmespath", {}),
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


class TestVerifyJmespath:
    """``verify.jmespath``: a JMESPath expression per key, mapped to a value it
    must equal (anything but an object) or to a matcher (an object, always)."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("abc", id="string"),
            pytest.param(3, id="int"),
            pytest.param(1.5, id="float"),
            pytest.param(True, id="bool"),
            pytest.param(None, id="null"),
            # An object inside an array is a literal, not a matcher.
            pytest.param([1, "a", {"page": 1}], id="array"),
            pytest.param("{{ user_id }}", id="template"),
        ],
    )
    def test_non_object_is_a_value(self, value):
        assert Verify(jmespath={"data.id": value}).jmespath == {"data.id": value}

    @pytest.mark.parametrize(
        ("matcher", "fields_set"),
        [
            pytest.param({"eq": {"page": 1}}, {"eq"}, id="eq-object"),
            pytest.param({"gt": 0, "lt": 100.5}, {"gt", "lt"}, id="range"),
            pytest.param({"contains": "new", "length": 2}, {"contains", "length"}, id="contains-and-length"),
            pytest.param({"matches": "^A", "not_matches": "z$"}, {"matches", "not_matches"}, id="patterns"),
            pytest.param({"type": "integer"}, {"type"}, id="type"),
            # A null operand is compared with, and set: it is not "no check".
            pytest.param({"ne": None}, {"ne"}, id="ne-null"),
            pytest.param({"eq": None}, {"eq"}, id="eq-null"),
            pytest.param({"contains": None, "not_contains": None}, {"contains", "not_contains"}, id="contains-null"),
            pytest.param({"gt": "{{ low }}", "type": "{{ kind }}", "length": "{{ n }}"}, {"gt", "type", "length"}, id="templates"),
        ],
    )
    def test_object_is_a_matcher(self, matcher, fields_set):
        parsed = Verify(jmespath={"x": matcher}).jmespath["x"]
        assert isinstance(parsed, JMESPathMatcher)
        assert parsed.model_fields_set == fields_set

    def test_namespace_is_the_object_it_stands_for(self):
        """A template over ``vars`` renders an object as a SimpleNamespace,
        which stands for that object: validated as it stands, a matcher, and
        under eq, compared as the object. (Rendered where a value was declared,
        it is that value: `test_rendered_value_stays_a_value`.)"""
        assert Verify(jmespath={"x": SimpleNamespace(gt=0)}).jmespath["x"] == JMESPathMatcher(gt=0)
        assert Verify(jmespath={"x": {"eq": SimpleNamespace(page=1, tags=[SimpleNamespace(a=1)])}}).jmespath["x"].eq == {"page": 1, "tags": [{"a": 1}]}

    def test_literal_object_hints_at_eq(self):
        """An object meant for equality is a matcher with unknown keys: refused
        loudly, pointing at eq, not with pydantic's "extra inputs"."""
        with pytest.raises(ValidationError, match=r"'page' is not one of its keys .*; to compare with an object, give it as eq") as exc_info:
            Verify(jmespath={"meta": {"page": 1}})
        assert_error_types(exc_info, "value_error", at="meta")

    def test_empty_object_hints_at_eq(self):
        with pytest.raises(ValidationError, match=r'must set at least one of: eq, .*; to compare with an empty object, write \{"eq": \{\}\}'):
            Verify(jmespath={"meta": {}})

    @pytest.mark.parametrize(
        ("literal", "error_type", "at"),
        [
            pytest.param({"type": "admin"}, "literal_error", "type", id="type"),
            pytest.param({"length": "long"}, "int_type", "length", id="length"),
            pytest.param({"gt": None}, "value_error", "matcher", id="null-operand"),
        ],
    )
    def test_literal_object_of_matcher_keys_hints_at_eq(self, literal, error_type, at):
        """An object meant for equality whose keys are all a matcher's fails as
        that matcher: its own errors stay, and the eq hint is one more beside
        them, at the object."""
        with pytest.raises(ValidationError) as exc_info:
            Verify(jmespath={"user": literal})
        assert_error_types(exc_info, error_type, at=at)
        hints = [error for error in exc_info.value.errors() if error["type"] == "jmespath_matcher"]
        assert [(error["loc"], error["msg"]) for error in hints] == [
            (("jmespath", "user", "matcher"), 'An object here is a matcher; to compare with an object, give it as eq: {"eq": {...}}')
        ]

    def test_empty_object_gets_its_own_hint_only(self):
        with pytest.raises(ValidationError) as exc_info:
            Verify(jmespath={"meta": {}})
        assert "jmespath_matcher" not in [error["type"] for error in exc_info.value.errors()]

    @pytest.mark.parametrize("key", ["gt", "ge", "lt", "le", "matches", "not_matches", "type", "length"])
    def test_null_is_refused_where_it_is_no_operand(self, key):
        with pytest.raises(ValidationError, match=f"matcher's {key} must not be null"):
            Verify(jmespath={"x": {key: None}})

    @pytest.mark.parametrize(
        ("matcher", "error_type"),
        [
            # A JSON number: never a bool, never text.
            pytest.param({"gt": True}, "int_type", id="gt-bool"),
            pytest.param({"lt": "5"}, "int_type", id="lt-numeric-text"),
            pytest.param({"length": 1.0}, "int_type", id="length-float"),
            pytest.param({"length": -1}, "greater_than_equal", id="length-negative"),
            pytest.param({"type": "int"}, "literal_error", id="type-unknown"),
            pytest.param({"matches": "["}, "value_error", id="matches-invalid-regex"),
            pytest.param({"eq": 1, "equals": 2}, "value_error", id="unknown-key-beside-a-known-one"),
        ],
    )
    def test_invalid_matcher_rejected(self, matcher, error_type):
        with pytest.raises(ValidationError) as exc_info:
            Verify(jmespath={"x": matcher})
        assert_error_types(exc_info, error_type)

    @pytest.mark.parametrize(
        ("key", "message"),
        [
            pytest.param("items[", "Invalid JMESPath expression", id="invalid"),
            pytest.param("", "Invalid JMESPath expression", id="empty"),
            # Keys are never rendered: `{{` is not JMESPath, and the error says why.
            pytest.param("data.{{ field }}", "a key is never rendered, only the value it maps to, so it cannot hold a template", id="template"),
        ],
    )
    def test_key_is_a_jmespath_expression(self, key, message):
        with pytest.raises(ValidationError, match=re.escape(message)) as exc_info:
            Verify(jmespath={key: 1})
        assert_error_types(exc_info, "value_error", at="[key]")

    @pytest.mark.parametrize(
        ("rendered", "expected"),
        [
            # An object whose keys happen to be a matcher's is still the value.
            pytest.param({"type": "object"}, {"type": "object"}, id="matcher-keys"),
            pytest.param({"eq": None}, {"eq": None}, id="null-operand"),
            pytest.param(SimpleNamespace(page=1, tags=[SimpleNamespace(a=1)]), {"page": 1, "tags": [{"a": 1}]}, id="namespace"),
            pytest.param([SimpleNamespace(gt=0)], [{"gt": 0}], id="namespace-in-array"),
            pytest.param(None, None, id="null"),
        ],
    )
    def test_rendered_value_stays_a_value(self, rendered, expected):
        """What was declared decides: a value's template that renders an object
        renders the value to compare with, never a matcher (`validate_rendered_verify`)."""
        declared = Verify(jmespath={"x": "{{ v }}", "y": {"gt": "{{ low }}"}})
        verify = validate_rendered_verify(declared, {"jmespath": {"x": rendered, "y": {"gt": 0}}})
        assert verify.jmespath == {"x": expected, "y": JMESPathMatcher(gt=0)}

    def test_rendered_matcher_gets_no_hint(self):
        """A matcher declared as one can fail once rendered only by an operand;
        the hint for an object meant as a value would be noise."""
        declared = Verify(jmespath={"x": {"type": "{{ kind }}"}})
        with pytest.raises(ValidationError) as exc_info:
            validate_rendered_verify(declared, {"jmespath": {"x": {"type": "admin"}}})
        assert_error_types(exc_info, "literal_error", at="type")
        assert "jmespath_matcher" not in [error["type"] for error in exc_info.value.errors()]

    def test_non_json_value_rejected(self):
        """A value a template rendered must be JSON: a tuple is not an array."""
        with pytest.raises(ValidationError) as exc_info:
            Verify(jmespath={"x": (1, 2)})
        assert_error_types(exc_info, "invalid-json-value", at="x")


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
            pytest.param("schemas/user.json", "schemas/user.json", id="path"),
            # Kept as written: a Path would fold the pointer's "//" (an empty
            # key) and drop its trailing "/" (one more).
            pytest.param("./openapi.json#/components//x/", "./openapi.json#/components//x/", id="path-with-pointer"),
            pytest.param(Path("schemas/user.json"), str(Path("schemas/user.json")), id="path-object"),
            pytest.param("{{ schema_path }}", "{{ schema_path }}", id="template"),
            pytest.param("{{ name }}.json#/$defs/User", "{{ name }}.json#/$defs/User", id="template-with-pointer"),
        ],
    )
    def test_schema_forms(self, schema, expected):
        assert ResponseBody(schema=schema).schema == expected

    @pytest.mark.parametrize(
        ("schema", "message"),
        [
            pytest.param("https://api.example.com/openapi.json#/components/schemas/User", "remote schemas are not fetched", id="remote"),
            pytest.param("openapi.json#User", "must be a JSON pointer starting with '/'", id="not-a-pointer"),
        ],
    )
    def test_schema_file_reference_refused_at_load(self, schema, message):
        """Wiring only: the exhaustive cases live in test_type_validators.py."""
        with pytest.raises(ValidationError, match=re.escape(message)):
            ResponseBody(schema=schema)

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
