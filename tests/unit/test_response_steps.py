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
from pytest_httpchain.models import JSON_TYPE_NAMES, JMESPathSave, Verify
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION
from pytest_httpchain.response_steps import is_json_type, process_save, process_verify

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

    @pytest.mark.parametrize(
        ("response", "message"),
        [
            pytest.param(NOT_JSON, "response is not valid JSON", id="text"),
            # The schema check shares the jmespath check's parse (one per
            # verify step), and so how it fails: json raises a bare ValueError
            # for an int past Python's digit limit, and RecursionError for
            # valid JSON nested past what it can parse. Both escaped as a raw
            # traceback here.
            pytest.param(httpx.Response(200, content=b'{"a": ' + b"1" * 5000 + b"}"), "response is not valid JSON", id="int-too-long"),
            pytest.param(httpx.Response(200, content=b"[" * 200_000 + b"]" * 200_000), "response JSON is nested too deeply to parse", id="too-deep"),
        ],
    )
    def test_non_json_response_fails_cleanly(self, response, message):
        with pytest.raises(VerificationError, match=f"^Cannot validate schema, {message}: "):
            process_verify(Verify(body=ResponseBody(schema={"type": "object"})), response)

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


BODY = {
    "data": {"id": 42, "name": "Alice", "tags": ["new", "sale"], "meta": {"page": 1}},
    "items": [{"name": "Apple", "price": 1.5}, {"name": "Banana", "price": 0.25}, {"name": "Cherry", "price": 12}],
    "active": True,
    "count": 3,
    "deleted_at": None,
}
JSON_BODY = httpx.Response(200, json=BODY)


def _verify_jmespath(jmespath: dict, response: httpx.Response = JSON_BODY) -> None:
    process_verify(Verify(jmespath=jmespath), response)


class TestJmespath:
    """``verify.jmespath``: each expression's value against a value it must
    equal, or against a matcher every key of which must hold."""

    @pytest.mark.parametrize(
        "jmespath",
        [
            pytest.param({"data.id": 42, "data.name": "Alice", "active": True, "deleted_at": None}, id="scalars"),
            pytest.param({"data.tags": ["new", "sale"], "length(items)": 3, "items[?price > `1`].name": ["Apple", "Cherry"]}, id="arrays-and-functions"),
            # JSON equality: an int equals the float of the same value, deeply.
            pytest.param({"count": 3.0, "items[2]": {"eq": {"name": "Cherry", "price": 12.0}}}, id="int-equals-float"),
            # A missing path extracts null, as a null does.
            pytest.param({"no.such.path": None, "data.missing": {"type": "null"}}, id="missing-path-is-null"),
            pytest.param({"data.meta": {"eq": {"page": 1}}, "data.id": {"ne": 41}, "data": {"ne": None}}, id="eq-ne"),
            pytest.param({"items[0].price": {"gt": 1, "ge": 1.5, "lt": 2, "le": 1.5}, "count": {"ge": 3, "le": 3}}, id="orderings"),
            pytest.param({"data.name": {"contains": "lic", "not_contains": "bob"}}, id="contains-substring"),
            pytest.param({"data.tags": {"contains": "new", "not_contains": "old"}, "items[*].price": {"contains": 12.0}}, id="contains-element"),
            pytest.param({"data.meta": {"contains": "page", "not_contains": "size"}}, id="contains-key"),
            pytest.param({"data.name": {"matches": "^A", "not_matches": "^B"}}, id="patterns"),
            pytest.param({"data.tags": {"length": 2}, "data.name": {"length": 5}, "data.meta": {"length": 1}}, id="lengths"),
            pytest.param(
                {
                    "data.name": {"type": "string"},
                    "count": {"type": "integer"},
                    "items[0].price": {"type": "number"},
                    "items[2].price": {"type": "number"},
                    "active": {"type": "boolean"},
                    "data.tags": {"type": "array"},
                    "data": {"type": "object"},
                    "deleted_at": {"type": "null"},
                },
                id="types",
            ),
            pytest.param({"items[*].price": {"contains": 12, "length": 3, "type": "array"}}, id="several-keys"),
        ],
    )
    def test_passes(self, jmespath):
        _verify_jmespath(jmespath)

    @pytest.mark.parametrize(
        ("jmespath", "message"),
        [
            pytest.param({"data.id": 43}, "JMESPath 'data.id' doesn't match: expected 43, got 42", id="value"),
            # JSON equality: a boolean is never a number, text never a number.
            pytest.param({"active": 1}, "JMESPath 'active' doesn't match: expected 1, got true", id="bool-is-not-1"),
            pytest.param({"count": True}, "JMESPath 'count' doesn't match: expected true, got 3", id="1-is-not-bool"),
            pytest.param({"data.id": "42"}, "JMESPath 'data.id' doesn't match: expected \"42\", got 42", id="text-is-not-a-number"),
            pytest.param({"data.tags": ["new"]}, 'JMESPath \'data.tags\' doesn\'t match: expected ["new"], got ["new", "sale"]', id="array"),
            pytest.param({"data.meta": {"eq": {"page": True}}}, 'JMESPath \'data.meta\' doesn\'t match: expected eq {"page": true}, got {"page": 1}', id="nested-bool-is-not-1"),
            # null cannot tell a missing path from a null value either way.
            pytest.param({"data.missing": {"ne": None}}, "JMESPath 'data.missing' doesn't match: expected ne null, got null", id="ne-null-on-missing"),
            pytest.param({"deleted_at": {"ne": None}}, "JMESPath 'deleted_at' doesn't match: expected ne null, got null", id="ne-null-on-null"),
            pytest.param({"count": {"gt": 3}}, "JMESPath 'count' doesn't match: expected gt 3, got 3", id="gt"),
            pytest.param({"count": {"ge": 3.5}}, "JMESPath 'count' doesn't match: expected ge 3.5, got 3", id="ge"),
            pytest.param({"count": {"lt": 3}}, "JMESPath 'count' doesn't match: expected lt 3, got 3", id="lt"),
            pytest.param({"count": {"le": 2}}, "JMESPath 'count' doesn't match: expected le 2, got 3", id="le"),
            pytest.param({"data.name": {"contains": "Bob"}}, 'JMESPath \'data.name\' doesn\'t match: expected contains "Bob", got "Alice"', id="contains-substring"),
            pytest.param(
                {"data.tags": {"not_contains": "new"}}, 'JMESPath \'data.tags\' doesn\'t match: expected not_contains "new", got ["new", "sale"]', id="not-contains-element"
            ),
            # An element is found by JSON equality: true is not 1.
            pytest.param(
                {"items[*].price": {"contains": True}}, "JMESPath 'items[*].price' doesn't match: expected contains true, got [1.5, 0.25, 12]", id="contains-element-bool"
            ),
            pytest.param({"data.meta": {"contains": "size"}}, 'JMESPath \'data.meta\' doesn\'t match: expected contains "size", got {"page": 1}', id="contains-key"),
            pytest.param({"data.name": {"matches": "^B"}}, 'JMESPath \'data.name\' doesn\'t match: expected matches "^B", got "Alice"', id="matches"),
            pytest.param({"data.name": {"not_matches": "l"}}, 'JMESPath \'data.name\' doesn\'t match: expected not_matches "l", got "Alice"', id="not-matches"),
            # integer is a number written without a fraction; no number is a boolean.
            pytest.param({"items[0].price": {"type": "integer"}}, "JMESPath 'items[0].price' doesn't match: expected type integer, got 1.5 (number)", id="type-integer"),
            pytest.param({"active": {"type": "number"}}, "JMESPath 'active' doesn't match: expected type number, got true (boolean)", id="type-bool-is-no-number"),
            pytest.param({"data.missing": {"type": "string"}}, "JMESPath 'data.missing' doesn't match: expected type string, got null (null)", id="type-missing"),
            pytest.param({"data.tags": {"length": 3}}, 'JMESPath \'data.tags\' doesn\'t match: expected length 3, got ["new", "sale"] (length 2)', id="length"),
            # Every key must hold: the first that does not, in the matcher's order, fails.
            pytest.param({"count": {"lt": 10, "gt": 5}}, "JMESPath 'count' doesn't match: expected gt 5, got 3", id="all-keys-must-hold"),
            # Expressions run in the order written; the first failure is reported.
            pytest.param({"data.id": 42, "count": 4, "active": False}, "JMESPath 'count' doesn't match: expected 4, got 3", id="first-failing-expression"),
        ],
    )
    def test_mismatch_message(self, jmespath, message):
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath(jmespath)
        assert str(excinfo.value) == message

    @pytest.mark.parametrize(
        ("jmespath", "message"),
        [
            # A value the key cannot judge fails; it never passes vacuously.
            pytest.param({"data.name": {"gt": 0}}, "JMESPath 'data.name': gt needs a number, got \"Alice\" (string)", id="gt-on-string"),
            pytest.param({"active": {"le": 1}}, "JMESPath 'active': le needs a number, got true (boolean)", id="le-on-bool"),
            pytest.param({"data.missing": {"lt": 1}}, "JMESPath 'data.missing': lt needs a number, got null (null)", id="lt-on-missing"),
            pytest.param({"count": {"matches": "3"}}, "JMESPath 'count': matches needs a string, got 3 (number)", id="matches-on-number"),
            pytest.param({"count": {"not_matches": "3"}}, "JMESPath 'count': not_matches needs a string, got 3 (number)", id="not-matches-on-number"),
            pytest.param({"count": {"contains": 3}}, "JMESPath 'count': contains needs a string, array or object, got 3 (number)", id="contains-on-number"),
            pytest.param(
                {"data.missing": {"not_contains": "x"}}, "JMESPath 'data.missing': not_contains needs a string, array or object, got null (null)", id="not-contains-on-missing"
            ),
            pytest.param({"data.name": {"contains": 1}}, "JMESPath 'data.name': contains on a string needs a string to look for, got 1 (number)", id="contains-number-in-string"),
            pytest.param(
                {"data.meta": {"not_contains": None}},
                "JMESPath 'data.meta': not_contains on an object needs a key (a string) to look for, got null (null)",
                id="not-contains-null-key",
            ),
            pytest.param({"count": {"length": 1}}, "JMESPath 'count': length needs a string, array or object, got 3 (number)", id="length-on-number"),
        ],
    )
    def test_value_the_key_cannot_judge_fails(self, jmespath, message):
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath(jmespath)
        assert str(excinfo.value) == message

    @pytest.mark.parametrize(
        ("matcher", "message"),
        [
            # Template text a template rendered passes the template branch, as
            # verify.status's does: refused by name, never compared, before
            # the value is looked at.
            pytest.param({"gt": "{{ y }}"}, "gt must resolve to a number, got '{{ y }}'", id="gt"),
            pytest.param({"type": "{{ y }}"}, "type must resolve to one of string, number, integer, boolean, array, object, null, got '{{ y }}'", id="type"),
            pytest.param({"length": "{{ y }}"}, "length must resolve to a non-negative integer, got '{{ y }}'", id="length"),
            pytest.param({"matches": "[{{ y }}"}, "matches must resolve to a regular expression, got '[{{ y }}' (unterminated character set at position 0)", id="matches"),
        ],
    )
    def test_template_text_operand_is_refused(self, matcher, message):
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({"data.name": matcher})
        assert str(excinfo.value) == f"JMESPath 'data.name': {message}"

    def test_null_operand_is_an_element(self):
        _verify_jmespath({"list": {"contains": None}}, httpx.Response(200, json={"list": [1, None]}))
        with pytest.raises(VerificationError, match=r"expected not_contains null, got \[1, null\]"):
            _verify_jmespath({"list": {"not_contains": None}}, httpx.Response(200, json={"list": [1, None]}))

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(NOT_JSON, id="text"),
            pytest.param(httpx.Response(204), id="empty"),
            pytest.param(httpx.Response(200, content=b"\xff\xfe{"), id="not-utf8"),
            # json raises a bare ValueError for an int past Python's digit limit.
            pytest.param(httpx.Response(200, content=b'{"a": ' + b"1" * 5000 + b"}"), id="int-too-long"),
        ],
    )
    def test_non_json_body_fails_cleanly(self, response):
        with pytest.raises(VerificationError, match="^Cannot check verify.jmespath, response is not valid JSON: "):
            _verify_jmespath({"a": 1}, response)

    def test_body_nested_too_deeply_fails_cleanly(self):
        """json raises RecursionError, not a ValueError, for valid JSON nested
        past what it can parse: a stage failure, not a raw traceback."""
        response = httpx.Response(200, content=b"[" * 200_000 + b"]" * 200_000)
        with pytest.raises(VerificationError, match="^Cannot check verify.jmespath, response JSON is nested too deeply to parse: "):
            _verify_jmespath({"a": 1}, response)

    def test_json_null_body_is_null_everywhere(self):
        _verify_jmespath({"a": None, "@": {"type": "null"}}, httpx.Response(200, content=b"null"))

    @pytest.mark.parametrize(
        ("expression", "body", "reason"),
        [
            # jmespath's type error, its value shown as JSON, not as a repr.
            pytest.param("length(count)", BODY, "length() needs string or array or object, got 3 (number)", id="wrong-type"),
            pytest.param("keys(flags)", {"flags": [True, None]}, "keys() needs object, got [true, null] (array)", id="wrong-type-shown-as-json"),
            # Compiled at validation, but only called against a body.
            pytest.param("lenght(count)", BODY, "Unknown function: lenght()", id="unknown-function"),
            pytest.param("length(count, count)", BODY, "Expected 1 argument for function length(), received 2", id="argument-count"),
            # jmespath hands these to Python's math and `in` unchecked: json
            # reads 1e400 as inf, and NaN as it is.
            pytest.param("ceil(x)", b'{"x": 1e400}', "cannot convert float infinity to integer", id="ceil-of-inf"),
            pytest.param("floor(x)", b'{"x": NaN}', "cannot convert float NaN to integer", id="floor-of-nan"),
            pytest.param("contains(s, n)", {"s": "abc", "n": 1}, "'in <string>' requires string as left operand, not int", id="number-in-string"),
            # An expression reference is no JSON value to show.
            pytest.param("length(&a)", BODY, "length() needs string or array or object, got (not JSON: holds a JMESPath expression reference) (expref)", id="expref"),
            # An array argument's element: jmespath names the element alone,
            # by its Python type (int, str, bool), which read as if the
            # argument were that scalar.
            pytest.param("join(',', a)", {"a": ["x", 1]}, "join() needs array-string, got an array holding 1 (number)", id="element"),
            pytest.param("sum(a)", {"a": [1, None]}, "sum() needs array-number, got an array holding null (null)", id="element-null"),
            pytest.param("sort(a)", {"a": [1, True]}, "sort() needs array-string or array-number, got an array holding true (boolean)", id="element-bool"),
            pytest.param("min(a)", {"a": [[1]]}, "min() needs array-number or array-string, got an array holding [1] (array)", id="first-element"),
            # Of a type the function takes, but not the first element's.
            pytest.param("max(a)", {"a": [1, "x"]}, 'max() needs array-number or array-string, got an array of mixed types holding "x" (string)', id="mixed"),
            pytest.param(
                "join(',', to_array(&a))",
                BODY,
                "join() needs array-string, got an array holding (not JSON: holds a JMESPath expression reference) (expref)",
                id="element-expref",
            ),
            # What a *_by() expression gave for an element, by its type: the
            # error holds that value, or the first element itself.
            pytest.param("max_by(a, &k)", {"a": [{"k": 1}, {}]}, "max_by() needs its expression to give number or string for every element, got null for one", id="by-key"),
            pytest.param(
                "sort_by(a, &k)",
                {"a": [{"k": 1}, {"k": "x"}]},
                "sort_by() needs its expression to give number for every element, got string for one",
                id="by-key-unlike-first",
            ),
            pytest.param(
                "sort_by(a, &k)",
                {"a": [{"k": True}]},
                "sort_by() needs its expression to give string or number for every element, got boolean for one",
                id="by-first-key",
            ),
            pytest.param("sort_by(a, &k)", {"a": {"k": 1}}, 'sort_by() needs array, got {"k": 1} (object)', id="by-argument"),
        ],
    )
    def test_evaluation_error_fails_cleanly(self, expression, body, reason):
        """Compiled at validation, an expression can still fail against a body:
        a stage failure naming why, never a raw traceback."""
        response = httpx.Response(200, content=body) if isinstance(body, bytes) else httpx.Response(200, json=body)
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({expression: 1}, response)
        assert str(excinfo.value) == f"JMESPath {expression!r} cannot be evaluated against the response body: {reason}"

    def test_evaluation_error_cuts_the_value_it_names(self):
        """jmespath's type error held the value it refused whole, as a Python
        repr: a line tens of kilobytes long for keys() over a large array."""
        response = httpx.Response(200, json={"items": [{"name": "x" * 50, "flag": True, "gone": None}] * 200})
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({"keys(items)": []}, response)
        message = str(excinfo.value)
        assert message.startswith('JMESPath \'keys(items)\' cannot be evaluated against the response body: keys() needs object, got [{"name": "xxx')
        assert re.search(r"\.\.\. \(\d+ characters\) \(array\)$", message)
        assert len(message) < 400

    @pytest.mark.parametrize(
        ("expectation", "message"),
        [
            pytest.param(1, "doesn't match: expected 1, got (not JSON: holds a JMESPath expression reference)", id="value"),
            pytest.param({"length": 2}, "doesn't match: expected length 2, got (not JSON: holds a JMESPath expression reference) (length 1)", id="matcher"),
        ],
    )
    def test_expression_reference_result_fails_cleanly(self, expectation, message):
        """jmespath hands back an expression reference (``&a``) as it is from
        to_array(), which json cannot show: the failure reads without it."""
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({"to_array(&a)": expectation})
        assert str(excinfo.value) == f"JMESPath 'to_array(&a)' {message}"

    def test_expression_reference_is_typed_as_jmespath_types_it(self):
        """A key that cannot judge an expression reference names its type as
        JMESPath does, not by the Python class jmespath holds it in."""
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({"to_array(&a)[0]": {"gt": 1}})
        assert str(excinfo.value) == "JMESPath 'to_array(&a)[0]': gt needs a number, got (not JSON: holds a JMESPath expression reference) (expref)"

    @pytest.mark.parametrize(
        "verify",
        [
            pytest.param({"jmespath": {"data.id": 42, "count": {"gt": 1}, "active": True}}, id="jmespath"),
            pytest.param({"jmespath": {"data.id": 42}, "body": {"schema": {"type": "object"}}}, id="jmespath-and-schema"),
            pytest.param({"body": {"schema": {"type": "object"}}}, id="schema"),
        ],
    )
    def test_body_is_parsed_once_per_step(self, monkeypatch, verify):
        response = httpx.Response(200, json=BODY)
        calls = []
        parse = response.json
        monkeypatch.setattr(response, "json", lambda: calls.append(1) or parse())
        process_verify(Verify.model_validate(verify), response)
        assert len(calls) == 1

    def test_body_is_not_parsed_when_nothing_reads_it(self, monkeypatch):
        response = httpx.Response(200, content=b"not json")
        monkeypatch.setattr(response, "json", lambda: pytest.fail("parsed"))
        process_verify(Verify.model_validate({"status": 200, "body": {"contains": ["not"]}}), response)

    def test_int_too_long_to_show(self):
        """A template can render an int no conversion to text takes: the
        failure still reads, without it."""
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({"count": {"eq": 10**5000}})
        assert str(excinfo.value) == "JMESPath 'count' doesn't match: expected eq (a number too long to show), got 3"

    def test_long_values_are_cut(self):
        response = httpx.Response(200, json={"big": list(range(1000))})
        with pytest.raises(VerificationError) as excinfo:
            _verify_jmespath({"big": {"length": 1}}, response)
        message = str(excinfo.value)
        assert message.startswith("JMESPath 'big' doesn't match: expected length 1, got [0, 1, 2")
        assert re.search(r"\.\.\. \(\d+ characters\) \(length 1000\)$", message)
        assert len(message) < 300

    @pytest.mark.parametrize(
        ("verify", "message"),
        [
            # status, headers, jmespath, expressions, user_functions, body.schema, body text.
            pytest.param({"headers": {"X-Id": "8"}, "jmespath": {"count": 4}}, "Header 'X-Id' doesn't match", id="after-headers"),
            pytest.param({"jmespath": {"count": 4}, "expressions": [False]}, "JMESPath 'count'", id="before-expressions"),
            pytest.param({"jmespath": {"count": 4}, "body": {"schema": {"type": "array"}}}, "JMESPath 'count'", id="before-body-schema"),
            pytest.param({"jmespath": {"count": 4}, "body": {"contains": ["nope"]}}, "JMESPath 'count'", id="before-body-text"),
        ],
    )
    def test_order_within_a_verify_step(self, verify, message):
        response = httpx.Response(200, json=BODY, headers={"x-id": "7"})
        with pytest.raises(VerificationError, match=f"^{re.escape(message)}"):
            process_verify(Verify.model_validate(verify), response)


@pytest.mark.parametrize(
    ("value", "types"),
    [
        pytest.param("s", {"string"}, id="string"),
        pytest.param(1, {"number", "integer"}, id="int"),
        pytest.param(1.0, {"number"}, id="float"),
        pytest.param(True, {"boolean"}, id="bool"),
        pytest.param([], {"array"}, id="array"),
        pytest.param({}, {"object"}, id="object"),
        pytest.param(None, {"null"}, id="null"),
    ],
)
def test_is_json_type(value, types):
    assert {name for name in JSON_TYPE_NAMES if is_json_type(value, name)} == types
