"""response_steps: the meaning of one verify or save step.

The passing side of each step type runs end to end in
tests/integration/test_verify.py and test_save.py; this pins the failure
messages and the edge cases a mock server cannot produce cheaply.
"""

import json
import re
from collections import ChainMap
from http import HTTPStatus

import httpx
import pytest

from pytest_httpchain.errors import SaveError, VerificationError
from pytest_httpchain.models import JMESPathSave, Verify
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION
from pytest_httpchain.response_steps import process_save, process_verify

NOT_JSON = httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})


def test_jmespath_save_rejects_non_json_response():
    with pytest.raises(SaveError, match="response is not valid JSON"):
        process_save(JMESPathSave(jmespath={"value": "key"}), NOT_JSON, ChainMap())


class TestStatus:
    """``verify.status``: a code, a class, or a list of them any one of which passes."""

    @pytest.mark.parametrize(
        ("status", "actual"),
        [
            pytest.param(200, 200, id="code"),
            pytest.param(HTTPStatus.CREATED, 201, id="standard-code"),
            pytest.param(499, 499, id="nonstandard-code"),
            pytest.param("2xx", 204, id="class"),
            pytest.param("2XX", 299, id="class-uppercase"),
            pytest.param("1xx", 101, id="class-lowest"),
            pytest.param("5xx", 599, id="class-highest"),
            pytest.param([200, 201], 201, id="list-of-codes"),
            pytest.param(["2xx", 304], 304, id="list-code-matches"),
            pytest.param(["2xx", 304], 200, id="list-class-matches"),
            pytest.param([404], 404, id="list-of-one"),
        ],
    )
    def test_passes(self, status, actual):
        process_verify(Verify(status=status), httpx.Response(actual))

    @pytest.mark.parametrize(
        ("status", "actual", "message"),
        [
            pytest.param(200, 500, "expected 200, got 500", id="code"),
            # Shown as the number, never as `HTTPStatus.OK`.
            pytest.param(HTTPStatus.OK, 500, "expected 200, got 500", id="standard-code"),
            pytest.param("200", 500, "expected 200, got 500", id="stringified-code"),
            pytest.param([200, 201], 500, "expected one of [200, 201], got 500", id="list-of-codes"),
            pytest.param("2xx", 500, "expected 2xx, got 500", id="class"),
            # Shown in the one spelling the model keeps.
            pytest.param("2XX", 300, "expected 2xx, got 300", id="class-uppercase"),
            # The class is the hundreds digit, not a range around it.
            pytest.param("2xx", 199, "expected 2xx, got 199", id="class-just-below"),
            pytest.param(["2xx", 304], 500, "expected one of [2xx, 304], got 500", id="list-mixed"),
            pytest.param([404], 200, "expected one of [404], got 200", id="list-of-one"),
        ],
    )
    def test_mismatch_message(self, status, actual, message):
        with pytest.raises(VerificationError) as excinfo:
            process_verify(Verify(status=status), httpx.Response(actual))
        assert str(excinfo.value) == f"Status code doesn't match: {message}"

    @pytest.mark.parametrize(
        "status",
        [
            pytest.param("{{ y }}", id="whole"),
            # Refused even though 200 would have passed on the other entry.
            pytest.param([200, "{{ y }}"], id="list-entry"),
        ],
    )
    def test_template_text_is_refused(self, status):
        """A template can render to another template's text (``x`` saved as
        ``"{{ y }}"``), which re-validates as the field's template branch. It
        failed as a mismatch against `{{ y }}` before; it is no status at all."""
        with pytest.raises(VerificationError) as excinfo:
            process_verify(Verify(status=status), httpx.Response(200))
        assert str(excinfo.value) == "verify.status must resolve to a status code or a class such as 2xx, got '{{ y }}'"

    def test_status_zero_is_not_treated_as_absent(self):
        """The status gate is `is not None`, not truthiness."""
        verify = Verify.model_construct(status=0, headers={}, expressions=[], user_functions=[], body=ResponseBody())
        with pytest.raises(VerificationError, match="Status code doesn't match"):
            process_verify(verify, httpx.Response(200, json={}))


class TestBodySchema:
    # A byte-order mark, which editors on Windows write, failed the stage as invalid JSON.
    @pytest.mark.parametrize("prefix", ["", "\ufeff"], ids=["plain", "byte-order-mark"])
    def test_schema_file_is_loaded(self, tmp_path, prefix):
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(prefix + json.dumps({"type": "object", "required": ["id"]}), encoding="utf-8")
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


_COOKIE_RESPONSE = httpx.Response(200, headers=[("set-cookie", "sid=abc; Path=/"), ("set-cookie", "csrf=def; Path=/"), ("x-id", "7")])


@pytest.mark.parametrize(
    ("headers", "redaction", "message"),
    [
        pytest.param(
            {"Set-Cookie": "sid=guess; Path=/"},
            DEFAULT_REDACTION,
            # The expected value is a whole value of the header, so it is redacted too.
            "Header 'Set-Cookie' doesn't match: expected sid=[REDACTED]; Path=/, got sid=[REDACTED]; Path=/, csrf=[REDACTED]; Path=/",
            id="exact-match",
        ),
        pytest.param({"Authorization": "Bearer abc"}, DEFAULT_REDACTION, "Header 'Authorization' doesn't match: expected [REDACTED], got None", id="exact-match-absent"),
        pytest.param(
            {"Set-Cookie": {"contains": "Secure"}},
            DEFAULT_REDACTION,
            # A failed contains/matches operand is not in the value, so it
            # reveals nothing of it: shown as written.
            "Header 'Set-Cookie' (value: 'sid=[REDACTED]; Path=/, csrf=[REDACTED]; Path=/') doesn't contain 'Secure'",
            id="matcher",
        ),
        pytest.param(
            {"Set-Cookie": {"not_contains": "abc"}},
            DEFAULT_REDACTION,
            # A failed not_contains operand is part of the value: the session a
            # logout check says must be gone is the secret the value hides.
            "Header 'Set-Cookie' (value: 'sid=[REDACTED]; Path=/, csrf=[REDACTED]; Path=/') contains '[REDACTED]' while it shouldn't",
            id="not-contains-hidden-part",
        ),
        pytest.param(
            {"Set-Cookie": {"not_matches": "sid=a.c"}},
            DEFAULT_REDACTION,
            "Header 'Set-Cookie' (value: 'sid=[REDACTED]; Path=/, csrf=[REDACTED]; Path=/') matches '[REDACTED]' while it shouldn't",
            id="not-matches-hidden-part",
        ),
        # An operand whose text the shown value already holds reveals nothing more.
        pytest.param(
            {"Set-Cookie": {"not_contains": "csrf="}},
            DEFAULT_REDACTION,
            "Header 'Set-Cookie' (value: 'sid=[REDACTED]; Path=/, csrf=[REDACTED]; Path=/') contains 'csrf=' while it shouldn't",
            id="not-contains-shown-part",
        ),
        pytest.param(
            {"Set-Cookie": {"not_matches": "Path=/"}},
            DEFAULT_REDACTION,
            "Header 'Set-Cookie' (value: 'sid=[REDACTED]; Path=/, csrf=[REDACTED]; Path=/') matches 'Path=/' while it shouldn't",
            id="not-matches-shown-part",
        ),
        pytest.param({"X-Id": "8"}, DEFAULT_REDACTION, "Header 'X-Id' doesn't match: expected 8, got 7", id="unlisted-header"),
        pytest.param(
            {"Set-Cookie": "sid=guess; Path=/"},
            NO_REDACTION,
            "Header 'Set-Cookie' doesn't match: expected sid=guess; Path=/, got sid=abc; Path=/, csrf=def; Path=/",
            id="disabled",
        ),
        pytest.param(
            {"Set-Cookie": {"not_contains": "abc"}},
            NO_REDACTION,
            "Header 'Set-Cookie' (value: 'sid=abc; Path=/, csrf=def; Path=/') contains 'abc' while it shouldn't",
            id="disabled-not-contains",
        ),
    ],
)
def test_header_failure_message_redacts_the_value(headers, redaction, message):
    """A header check's message is printed with the report, so a redacted
    header's value is hidden there as it is in the report sections."""
    with pytest.raises(VerificationError) as excinfo:
        process_verify(Verify(headers=headers), _COOKIE_RESPONSE, redaction=redaction)
    assert str(excinfo.value) == message
