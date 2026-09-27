"""response_steps: the meaning of one verify or save step.

The passing side of each step type runs end to end in
tests/integration/test_verify.py and test_save.py; this pins the failure
messages and the edge cases a mock server cannot produce cheaply.
"""

import json
import re
from collections import ChainMap

import httpx
import pytest

from pytest_httpchain.errors import SaveError, VerificationError
from pytest_httpchain.models import JMESPathSave, Verify
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.response_steps import process_save, process_verify

NOT_JSON = httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})


def test_jmespath_save_rejects_non_json_response():
    with pytest.raises(SaveError, match="response is not valid JSON"):
        process_save(JMESPathSave(jmespath={"value": "key"}), NOT_JSON, ChainMap())


def test_status_zero_is_not_treated_as_absent():
    """The status gate is `is not None`, not truthiness."""
    verify = Verify.model_construct(status=0, headers={}, expressions=[], user_functions=[], body=ResponseBody())
    with pytest.raises(VerificationError, match="Status code doesn't match"):
        process_verify(verify, httpx.Response(200, json={}))


class TestBodySchema:
    def test_schema_file_is_loaded(self, tmp_path):
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps({"type": "object", "required": ["id"]}))
        process_verify(Verify(body=ResponseBody(schema=str(schema_path))), httpx.Response(200, json={"id": 123}))

    @pytest.mark.parametrize(
        "content",
        [
            None,
            # UnicodeDecodeError is a ValueError, not a JSONDecodeError, so a
            # narrower except let it escape the chain-abort machinery.
            b'{"type": "\xff\xfe object"}',
        ],
        ids=["missing", "non-utf8"],
    )
    def test_unreadable_schema_file_fails_cleanly(self, tmp_path, content):
        schema_path = tmp_path / "schema.json"
        if content is not None:
            schema_path.write_bytes(content)
        with pytest.raises(VerificationError, match="Error reading body schema file"):
            process_verify(Verify(body=ResponseBody(schema=str(schema_path))), httpx.Response(200, json={"id": 1}))

    def test_schema_file_that_is_not_a_json_schema_fails_cleanly(self, tmp_path):
        """Valid JSON, invalid JSON Schema: only compiling it can tell, so the
        failure must still be a clean verification error naming the file."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps({"type": 12}))
        with pytest.raises(VerificationError, match="Invalid JSON Schema in file"):
            process_verify(Verify(body=ResponseBody(schema=str(schema_path))), httpx.Response(200, json={}))

    def test_non_json_response_fails_cleanly(self):
        with pytest.raises(VerificationError, match="response is not valid JSON"):
            process_verify(Verify(body=ResponseBody(schema={"type": "object"})), NOT_JSON)

    @pytest.mark.parametrize(
        ("fmt", "value"),
        [
            ("email", "user@example.com"),
            ("ipv4", "192.0.2.1"),
            ("date", "2026-09-27"),
        ],
    )
    def test_conforming_format_passes(self, fmt, value):
        schema = {"type": "object", "properties": {"v": {"type": "string", "format": fmt}}}
        process_verify(Verify(body=ResponseBody(schema=schema)), httpx.Response(200, json={"v": value}))

    @pytest.mark.parametrize(
        ("fmt", "value"),
        [
            ("email", "not-an-email"),
            ("ipv4", "999.0.2.1"),
            ("date", "2026-13-45"),
            # `regex` means Python `re` syntax, as the docs say: a valid
            # ECMA-262 named group is not a 'regex'.
            ("regex", r"(?<year>\d{4})"),
            # The checker's `re.compile` raises OverflowError / RecursionError,
            # which jsonschema does not count as a format failure: they escaped
            # as a raw traceback with no request/response report.
            ("regex", "a{4294967296}"),
            ("regex", "(" * 5000 + ")" * 5000),
        ],
        ids=["email", "ipv4", "date", "regex-ecma-only", "regex-overflow", "regex-too-deep"],
    )
    def test_nonconforming_format_fails(self, fmt, value):
        """`format` was only an annotation (no format checker was passed), so
        the documented `"format": "email"` accepted any string."""
        schema = {"type": "object", "properties": {"v": {"type": "string", "format": fmt}}}
        expected = re.escape(f"Body schema validation failed: {value!r} is not a {fmt!r}")
        with pytest.raises(VerificationError, match=expected):
            process_verify(Verify(body=ResponseBody(schema=schema)), httpx.Response(200, json={"v": value}))

    @pytest.mark.parametrize(
        ("dialect", "checked"),
        [
            (None, True),
            ("https://json-schema.org/draft/2020-12/schema", True),
            ("http://json-schema.org/draft-07/schema#", False),
        ],
        ids=["default", "draft-2020-12", "draft-07"],
    )
    def test_checked_formats_follow_the_declared_dialect(self, dialect, checked):
        """The checker is the dialect's own, not jsonschema's catch-all
        FormatChecker, so `uuid` (defined from Draft 2019-09 on) is checked
        under the default 2020-12 but not under Draft 7, as the docs promise."""
        schema = {"type": "object", "properties": {"v": {"type": "string", "format": "uuid"}}}
        if dialect is not None:
            schema["$schema"] = dialect
        verify = Verify(body=ResponseBody(schema=schema))
        response = httpx.Response(200, json={"v": "nope"})
        if checked:
            with pytest.raises(VerificationError, match="'nope' is not a 'uuid'"):
                process_verify(verify, response)
        else:
            process_verify(verify, response)


class TestExpressions:
    def test_bools_pass(self):
        process_verify(Verify(expressions=[True, True]), httpx.Response(200))

    def test_false_fails(self):
        with pytest.raises(VerificationError, match="Expression 1 failed"):
            process_verify(Verify(expressions=[True, False, True]), httpx.Response(200))

    @pytest.mark.parametrize(
        ("value", "type_name"),
        [
            # `{{ response.status }}` against a 500: a value written where a
            # predicate belongs, truthy, and asserting nothing.
            pytest.param(500, "int", id="truthy-int"),
            # Forgetting the `{{ }}` leaves an always-truthy string behind.
            pytest.param("response.status == 200", "str", id="missing-braces"),
            pytest.param("", "str", id="falsy-str"),
            # Why `expressions` needs nothing from the carrier's rendered-away
            # guard: substitution rewrites the list element-wise, so a
            # rendered-away entry arrives here as None and fails the bool contract.
            pytest.param(None, "NoneType", id="rendered-away"),
        ],
    )
    def test_non_bool_is_rejected_naming_its_type(self, value, type_name):
        with pytest.raises(VerificationError, match=f"Verify expression 0 must evaluate to bool, got {type_name}"):
            process_verify(Verify(expressions=[value]), httpx.Response(200))


@pytest.mark.parametrize(
    ("matcher", "message"),
    [
        ({"contains": ["goodbye"]}, "Body doesn't contain 'goodbye'"),
        ({"not_contains": ["hello"]}, "Body contains 'hello' while it shouldn't"),
        ({"matches": ["z{3}"]}, r"Body doesn't match 'z\{3\}'"),
        ({"not_matches": ["wor"]}, "Body matches 'wor' while it shouldn't"),
    ],
    ids=["contains", "not_contains", "matches", "not_matches"],
)
def test_body_text_matcher_failure(matcher, message):
    with pytest.raises(VerificationError, match=message):
        process_verify(Verify(body=ResponseBody(**matcher)), httpx.Response(200, content=b"hello world"))
