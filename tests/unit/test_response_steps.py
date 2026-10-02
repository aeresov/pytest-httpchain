"""response_steps: the meaning of one verify or save step.

The passing side of each step type runs end to end in
tests/integration/test_verify.py and test_save.py; this pins the failure
messages and the edge cases a mock server cannot produce cheaply.
"""

import functools
import json
import re
from collections import ChainMap
from http import HTTPStatus

import httpx
import jsonschema
import msgpack
import pytest

from pytest_httpchain.body_schema import ReferenceBounds
from pytest_httpchain.errors import SaveError, VerificationError
from pytest_httpchain.models import JSON_TYPE_NAMES, JMESPathSave, RegexSave, SubstitutionsSave, UserFunctionsSave, Verify
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.models.types import convert_dict_to_namespace
from pytest_httpchain.redaction import DEFAULT_REDACTION, NO_REDACTION
from pytest_httpchain.response_steps import RenderFailure, RenderOutcome, is_json_type, process_save, process_verify
from pytest_httpchain.templates import TemplatesError
from pytest_httpchain.userfunc import UserFunctionError
from tests.unit import response_steps_test_helpers
from tests.unit.helpers import LOADABLE_BUT_DEEP, NOT_FOUND, TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK, nested, on_bounded_stack

NOT_JSON = httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})
# The decoder raises RecursionError, which is not a ValueError: a narrower except
# let it escape the chain-abort machinery, with no report section and no HAR entry.
# Tests parse it via `on_bounded_stack`, where it overflows on every interpreter.
TOO_DEEP_JSON = httpx.Response(200, content=TOO_DEEP_TO_PARSE, headers={"content-type": "application/json"})
# Past the int-to-str digit limit (4300 by default) json.loads raises a bare
# ValueError, not a JSONDecodeError.
INT_TOO_LONG = httpx.Response(200, content=b'{"a": ' + b"1" * 5000 + b"}")
# A scenario file may hold comments and trailing commas (jsonref/jsonc.py), but
# what comes over HTTP stays strict JSON: the server sent a broken body.
COMMENTED_JSON = httpx.Response(200, content=b'{"a": 1 // c\n}', headers={"content-type": "application/json"})
TRAILING_COMMA_JSON = httpx.Response(200, content=b'{"a": [1,]}', headers={"content-type": "application/json"})


def test_msgpack_save_and_verify_preserve_binary_with_content_type_or_override():
    packet = {"b": b"\x00\xff", "name": "device"}
    packed = msgpack.packb(packet, use_bin_type=True)
    response = httpx.Response(200, content=packed, headers={"content-type": "Application/Vnd.Device+Msgpack; version=1"})
    assert process_save(JMESPathSave(jmespath={"packet": "@", "binary": "b"}), response, ChainMap()) == {"packet": packet, "binary": b"\x00\xff"}
    process_verify(Verify.model_validate({"jmespath": {"b": {"eq": b"\x00\xff"}}}), response)

    mislabeled = httpx.Response(200, content=packed, headers={"content-type": "application/octet-stream"})
    assert process_save(JMESPathSave(jmespath={"binary": "b"}), mislabeled, ChainMap(), codec="msgpack") == {"binary": b"\x00\xff"}
    process_verify(Verify.model_validate({"jmespath": {"b": b"\x00\xff"}}), mislabeled, codec="msgpack")


def test_invalid_msgpack_is_step_failure():
    response = httpx.Response(200, content=b"\xc1", headers={"content-type": "application/msgpack"})
    with pytest.raises(SaveError, match="response is not valid MessagePack"):
        process_save(JMESPathSave(jmespath={"b": "b"}), response, ChainMap())
    with pytest.raises(VerificationError, match="response is not valid MessagePack"):
        process_verify(Verify(jmespath={"b": 1}), response)


def test_msgpack_mismatch_shows_nested_binary_as_hex():
    response = httpx.Response(200, content=msgpack.packb({"b": b"\x00\xff"}, use_bin_type=True), headers={"content-type": "application/msgpack"})
    with pytest.raises(VerificationError, match="0x00ff"):
        process_verify(Verify.model_validate({"jmespath": {"@": {"eq": {"b": "wrong"}}}}), response)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        pytest.param(NOT_JSON, "response is not valid JSON", id="not-json"),
        pytest.param(TOO_DEEP_JSON, "response JSON is nested too deeply to parse", id="too-deep"),
        pytest.param(INT_TOO_LONG, "response is not valid JSON", id="int-too-long"),
        pytest.param(COMMENTED_JSON, "response is not valid JSON", id="comment"),
        pytest.param(TRAILING_COMMA_JSON, "response is not valid JSON", id="trailing-comma"),
    ],
)
def test_jmespath_save_rejects_non_json_response(response, message):
    """In the words a verify step uses for the same body (`_JsonBody`)."""
    with pytest.raises(SaveError, match=f"^Cannot extract variables, {message}: "):
        on_bounded_stack(process_save, JMESPathSave(jmespath={"value": "key"}), response, ChainMap())


@pytest.mark.parametrize(
    ("expression", "body", "reason"),
    [
        pytest.param("length(id)", {"id": 5}, "length() needs string or array or object, got 5 (number)", id="jmespath-type-error"),
        pytest.param("lenght(id)", {"id": 5}, "Unknown function: lenght()", id="unknown-function"),
        # Python's, from what jmespath hands its functions unchecked: json
        # reads 1e400 as inf.
        pytest.param("contains(s, n)", {"s": "abc", "n": 1}, "'in <string>' requires string as left operand, not int", id="number-in-string"),
        pytest.param("ceil(x)", b'{"x": 1e400}', "cannot convert float infinity to integer", id="ceil-of-inf"),
    ],
)
def test_jmespath_save_evaluation_error_fails_cleanly(expression, body, reason):
    """An expression that cannot be evaluated against this body is a save
    failure naming why, which `retry.on: save` retries, never a traceback."""
    response = httpx.Response(200, content=body) if isinstance(body, bytes) else httpx.Response(200, json=body)
    with pytest.raises(SaveError) as excinfo:
        process_save(JMESPathSave(jmespath={"v": expression}), response, ChainMap())
    assert str(excinfo.value) == f"Error saving variable v: {reason}"
    assert excinfo.value.retryable is True


PAGE = httpx.Response(
    200,
    text='<form><input name="csrf" value="tok-1"></form>\n<p>Order #42</p>\n<a href="?id=7">7</a> <a href="?id=8">8</a>',
    headers={"content-type": "text/html; charset=utf-8"},
)


def _save_regex(entry, response: httpx.Response = PAGE):
    """What one ``save.regex`` entry, as declared (and so as rendered: no
    template in it), saves from ``response``."""
    return process_save(RegexSave.model_validate({"regex": {"v": entry}}), response, ChainMap())["v"]


class TestRegexSave:
    """``save.regex``: values from a body that is not JSON."""

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            # Group 1 when the pattern has groups, else the whole match.
            pytest.param('name="csrf" value="([^"]+)"', "tok-1", id="first-group"),
            pytest.param("Order #\\d+", "Order #42", id="whole-match"),
            pytest.param("(Order) #(\\d+)", "Order", id="first-of-several"),
            pytest.param("Order #(?P<id>\\d+)", "42", id="named-group-is-numbered"),
            # (?:...) is not a group.
            pytest.param("(?:Order) #\\d+", "Order #42", id="non-capturing"),
            pytest.param({"pattern": "Order #(?P<id>\\d+)", "group": "id"}, "42", id="group-name"),
            pytest.param({"pattern": "(Order) #(\\d+)", "group": 2}, "42", id="group-number"),
            pytest.param({"pattern": "(Order) #(\\d+)", "group": 0}, "Order #42", id="group-zero"),
            # The first match only: there are two ids.
            pytest.param("id=(\\d+)", "7", id="first-match"),
            pytest.param({"pattern": "id=(\\d+)", "all": True}, ["7", "8"], id="all"),
            pytest.param({"pattern": "id=\\d+", "all": True}, ["id=7", "id=8"], id="all-whole-matches"),
            pytest.param({"pattern": "(?P<k>id)=(?P<v>\\d+)", "group": "v", "all": True}, ["7", "8"], id="all-group-name"),
            # Nothing matching is a value with all: an empty list, not an error.
            pytest.param({"pattern": "sku=(\\d+)", "all": True}, [], id="all-without-a-match"),
            # A group that took no part in its match is None, as re has it.
            pytest.param("Order #(\\d+)|(pending)", "42", id="alternative-that-matched"),
            pytest.param({"pattern": "(pending)|Order #(\\d+)", "group": 1}, None, id="group-that-did-not-take-part"),
            # re.search, flags inline: `.` stops at a newline unless (?s).
            pytest.param({"pattern": "</form>(.*)</p>", "all": True}, [], id="dot-stops-at-a-newline"),
            pytest.param("(?s)</form>(.*)</p>", "\n<p>Order #42", id="dotall-inline-flag"),
            pytest.param("(?i)ORDER #(\\d+)", "42", id="ignorecase-inline-flag"),
        ],
    )
    def test_saves(self, entry, expected):
        assert _save_regex(entry) == expected

    def test_body_is_searched_as_decoded_text(self):
        """``response.text``, decoded by the charset the response declares, as
        ``verify.body`` reads it."""
        response = httpx.Response(200, content="café au lait".encode("latin-1"), headers={"content-type": "text/plain; charset=latin-1"})
        assert _save_regex("caf(.)", response) == "é"

    def test_every_entry_is_saved(self):
        save = RegexSave.model_validate({"regex": {"csrf": 'value="([^"]+)"', "order_id": "Order #(\\d+)", "ids": {"pattern": "id=(\\d+)", "all": True}}})
        assert process_save(save, PAGE, ChainMap()) == {"csrf": "tok-1", "order_id": "42", "ids": ["7", "8"]}

    @pytest.mark.parametrize(
        ("entry", "pattern"),
        [
            pytest.param("sku=(\\d+)", "sku=(\\d+)", id="pattern"),
            pytest.param({"pattern": "sku=(\\d+)", "group": 1}, "sku=(\\d+)", id="capture"),
        ],
    )
    def test_no_match_fails_naming_the_variable_and_the_pattern(self, entry, pattern):
        with pytest.raises(SaveError) as excinfo:
            _save_regex(entry)
        assert str(excinfo.value) == f"Error saving variable v: regex '{pattern}' does not match the response body"

    def test_step_stops_at_its_first_error(self):
        save = RegexSave.model_validate({"regex": {"first": "sku=(\\d+)", "second": "(nothing)"}})
        with pytest.raises(SaveError, match="^Error saving variable first: "):
            process_save(save, PAGE, ChainMap())

    # The rendered entries below hold template text a template rendered (a
    # value saved from a response), which each field's template branch takes
    # as it is: validation cannot judge them, so the save does.

    @pytest.mark.parametrize(
        ("pattern", "error"),
        [
            pytest.param("{{ ( }}", "missing ), unterminated subpattern at position 3", id="re-error"),
            pytest.param("a{4294967296}{{ x }}", "the repetition number is too large", id="overflow"),
            pytest.param("(" * 5000 + "{{ x }}" + ")" * 5000, "maximum recursion depth exceeded", id="recursion"),
        ],
    )
    def test_template_text_pattern_re_refuses_fails_cleanly(self, pattern, error):
        with pytest.raises(SaveError) as excinfo:
            _save_regex(pattern)
        assert str(excinfo.value) == f"Error saving variable v: pattern must resolve to a regular expression, got {pattern!r} ({error})"

    @pytest.mark.parametrize(
        ("capture", "message"),
        [
            pytest.param(
                {"pattern": "{{ x }}(a)", "group": 2},
                "regex '{{ x }}(a)' has no group 2 (it has 1 group; 0 is the whole match)",
                id="group-number",
            ),
            pytest.param({"pattern": "{{ x }}(a)", "group": "id"}, "regex '{{ x }}(a)' has no group named 'id' (it has no named groups)", id="group-name"),
            # Checked before any match is tried: all finding nothing does not
            # hide it behind an empty list.
            pytest.param(
                {"pattern": "{{ x }}(a)", "group": 2, "all": True},
                "regex '{{ x }}(a)' has no group 2 (it has 1 group; 0 is the whole match)",
                id="group-number-with-all",
            ),
            pytest.param({"pattern": "(a)", "group": "{{ y }}"}, "group must resolve to a group's number or name, got '{{ y }}'", id="group-template-text"),
            pytest.param({"pattern": "(a)", "all": "{{ y }}"}, "all must resolve to true or false, got '{{ y }}'", id="all-template-text"),
        ],
    )
    def test_rendered_capture_that_cannot_be_saved_fails_cleanly(self, capture, message):
        with pytest.raises(SaveError) as excinfo:
            _save_regex(capture)
        assert str(excinfo.value) == f"Error saving variable v: {message}"


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
            TOO_DEEP_TO_PARSE,
        ],
        ids=["missing", "non-utf8", "too-deep"],
    )
    def test_unreadable_schema_file_fails_cleanly(self, tmp_path, content):
        schema_path = tmp_path / "schema.json"
        if content is not None:
            schema_path.write_bytes(content)
        with pytest.raises(VerificationError, match="Error reading body schema file"):
            on_bounded_stack(process_verify, Verify(body=ResponseBody(schema=str(schema_path))), httpx.Response(200, json={"id": 1}))

    def test_schema_file_that_is_not_a_json_schema_fails_cleanly(self, tmp_path):
        """Valid JSON, invalid JSON Schema: only compiling it can tell, so the
        failure must still be a clean verification error naming the file."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps({"type": 12}))
        with pytest.raises(VerificationError, match="Invalid JSON Schema in file"):
            process_verify(Verify(body=ResponseBody(schema=str(schema_path))), httpx.Response(200, json={}))

    @pytest.mark.parametrize(
        "schema",
        [
            # The meta-schema's `format: regex` check declares only re.error, so
            # a pattern that re.compile rejects any other way escaped the stage raw.
            {"type": "string", "pattern": "a{4294967296}"},
            {"type": "string", "pattern": "(" * 1000 + ")" * 1000},
            # No regex involved: the meta-validator itself recurses too deep, so
            # a tolerant regex format checker alone would not cover it.
            functools.reduce(lambda inner, _: {"not": inner}, range(500), {"type": "string"}),
            # The meta-check fails cleanly, but the error's str() pretty-prints
            # the deep `type` value, inside the except clause.
            {"type": functools.reduce(lambda inner, _: [inner], range(1_000), [])},
        ],
        ids=["pattern-overflow", "pattern-nesting", "schema-nesting", "error-text-nesting"],
    )
    def test_schema_file_whose_meta_check_crashes_fails_cleanly(self, tmp_path, schema):
        """A meta-check that raises something other than SchemaError must still
        fail the stage as a verification error naming the file, not a bare
        traceback with no request/response report."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps(schema))
        with pytest.raises(VerificationError, match=r"Invalid JSON Schema in file '.*schema\.json': "):
            process_verify(Verify(body=ResponseBody(schema=str(schema_path))), httpx.Response(200, json="x"))

    @pytest.mark.parametrize(
        ("pattern", "error"),
        [
            pytest.param("a{4294967296}", "the repetition number is too large", id="overflow"),
            pytest.param("(" * 5000 + ")" * 5000, "maximum recursion depth exceeded", id="recursion"),
        ],
    )
    def test_schema_file_pattern_re_cannot_compile_fails_its_check(self, tmp_path, pattern, error):
        """Checking the file's schema compiles its patterns, and re raises
        OverflowError or RecursionError, not a SchemaError, for one too big: it
        escaped the step, and took the step's other failures with it. An
        inline schema's is refused when the model is validated."""
        schema_path = tmp_path / "schema.json"
        schema_path.write_text(json.dumps({"type": "object", "properties": {"a": {"type": "string", "pattern": pattern}}}))
        verify = {"status": 201, "body": {"schema": str(schema_path), "contains": ["zzz"]}}
        assert str(_failure(verify, httpx.Response(200, json={"a": "x"}))).split("\n") == [
            "3 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            f"  2. Invalid JSON Schema in file '{schema_path}': {error}",
            "  3. Body doesn't contain 'zzz'",
        ]

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

    # json parses a body nested a thousand levels deep, on every platform, but
    # jsonschema recurses on it, pretty-printing the value that failed or
    # descending into it: a RecursionError escaped as a raw traceback, past the
    # report. And once every check runs, past an unrelated status failure
    # found first. (Five thousand levels is past what the decoder parses on
    # Windows, where it fails as a body too deep to parse instead.)
    DEEP = httpx.Response(200, content=TOO_DEEP_TO_WALK)

    def test_violation_in_a_body_too_deep_to_show(self):
        message = str(_failure({"status": 201, "body": {"schema": {"type": "object"}}}, self.DEEP))
        first, status, schema, *rest = message.split("\n")
        assert (first, status) == ("2 verification checks failed:", "  1. Status code doesn't match: expected 201, got 200")
        assert schema.startswith("  2. Body schema validation failed: [[[[")
        assert schema.endswith("]]]] is not of type 'object'")
        assert rest == ["", "     Failed validating 'type' at $: the value is nested too deeply to show"]

    def test_body_too_deep_to_validate(self):
        """A schema recursing as deep as the body cannot be checked on it."""
        schema = {"type": "array", "items": {"$ref": "#"}}
        with pytest.raises(VerificationError, match="^Cannot validate schema, response or schema is nested too deeply: maximum recursion depth exceeded"):
            process_verify(Verify(body=ResponseBody(schema=schema)), self.DEEP)

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


class TestBodySchemaFileWithPointer:
    """A schema taken out of a document by a JSON pointer, its references
    resolved across the document and into local files (`body_schema`), each
    problem one failure naming the file and the pointer."""

    OPENAPI = {
        "openapi": "3.1.0",
        "components": {
            "schemas": {
                "User": {
                    "type": "object",
                    "required": ["id", "email"],
                    "properties": {"id": {"type": "integer"}, "email": {"$ref": "common.json#/$defs/Email"}, "role": {"$ref": "#/components/schemas/Role"}},
                },
                "Role": {"enum": ["admin", "user"]},
                "Invalid": {"type": 12},
                "Missing": {"$ref": "missing.json"},
                "Nowhere": {"$ref": "#/components/schemas/Nobody"},
                "Pair": {"prefixItems": [{"type": "string"}, {"type": "integer"}]},
                "ByName": {"$ref": "#/components/schemas/Pair/prefixItems/first"},
            }
        },
    }

    @pytest.fixture
    def api(self, tmp_path):
        """The OpenAPI document one directory down from the scenario's, a file
        its schemas reference beside it."""
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "openapi.json").write_text(json.dumps(self.OPENAPI))
        (tmp_path / "api" / "common.json").write_text(json.dumps({"$defs": {"Email": {"type": "string", "format": "email"}}}))
        return tmp_path

    def _verify(self, schema, body, scenario_dir, *, ref_root=None, status=None):
        verify = {"body": {"schema": schema}, **({"status": status} if status else {})}
        process_verify(Verify.model_validate(verify), httpx.Response(200, json=body), scenario_dir, ref_bounds=ReferenceBounds(ref_root, 3))

    def _failure(self, schema, body, scenario_dir, **kwargs):
        with pytest.raises(VerificationError) as excinfo:
            self._verify(schema, body, scenario_dir, **kwargs)
        return str(excinfo.value)

    def test_component_passes(self, api):
        self._verify("api/openapi.json#/components/schemas/User", {"id": 1, "email": "a@example.com", "role": "user"}, api, ref_root=api)

    def test_violation_reads_as_any_schema_violation(self, api):
        """The schema path is the target's own: jsonschema does not put the
        $ref that reached it in the path."""
        message = self._failure("api/openapi.json#/components/schemas/User", {"id": 1, "email": "a@example.com", "role": "root"}, api)
        assert message.startswith("Body schema validation failed: 'root' is not one of ['admin', 'user']\n\nFailed validating 'enum' in schema['properties']['role']:")

    def test_format_is_checked_in_a_referenced_file(self, api):
        """c00053f's format checking applies to what a reference reaches."""
        message = self._failure("api/openapi.json#/components/schemas/User", {"id": 1, "email": "nope"}, api)
        assert message.startswith("Body schema validation failed: 'nope' is not a 'email'")

    @pytest.mark.parametrize(
        ("pointer", "reason"),
        [
            pytest.param("/components/schemas/Nobody", "'#/components/schemas' has no key 'Nobody'", id="missing-key"),
            pytest.param("/components/schemas/User/required/2", "'#/components/schemas/User/required' is an array of 2, with no item '2'", id="index-out-of-range"),
            pytest.param("/openapi/x", "'#/openapi' is a string, with nothing in it to select", id="into-a-string"),
        ],
    )
    def test_pointer_leading_nowhere(self, api, pointer, reason):
        message = self._failure(f"api/openapi.json#{pointer}", {}, api)
        assert message == f"Body schema pointer '#{pointer}' leads nowhere in file '{api / 'api' / 'openapi.json'}': {reason}"

    @pytest.mark.parametrize(
        ("name", "content", "error"),
        [
            # The OS's own words, the path quoted as Python quotes it (its
            # backslashes doubled, on Windows).
            pytest.param("nope.json", None, NOT_FOUND + ": {path}", id="missing"),
            pytest.param("bad.json", "{not json", "Expecting property name enclosed in double quotes", id="not-json"),
        ],
    )
    def test_unreadable_file_names_the_pointer(self, api, name, content, error):
        path = api / "api" / name
        if content is not None:
            path.write_text(content)
        message = self._failure(f"api/{name}#/components/schemas/User", {}, api)
        prefix = f"Error reading body schema file '{path}#/components/schemas/User': "
        assert message.startswith(prefix)
        assert re.match(error.format(path=re.escape(repr(str(path)))), message.removeprefix(prefix))

    def test_path_no_file_can_have_is_one_failure(self, tmp_path):
        """A NUL, one JSON \\u escape away: the OS call raises ValueError, which
        escaped as a traceback and took the step's other failures with it.
        Shown escaped."""
        message = self._failure("a\x00b.json", {}, tmp_path, status=201)
        # Escaped as repr() escapes it: a Windows path's backslashes too.
        shown = repr(str(tmp_path / "a\x00b.json"))[1:-1]
        assert message.split("\n") == [
            "2 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            f"  2. Error reading body schema file '{shown}': stat: embedded null character in path",
        ]

    @pytest.mark.parametrize(
        ("ref", "reason"),
        [
            pytest.param(
                "../../../../common.json",
                "exceeds the maximum parent traversal depth of 3 (httpchain_ref_parent_traversal_depth), as a scenario's $include path would",
                id="too-many-parents",
            ),
            pytest.param(
                "/etc/common.json",
                "is an absolute path, which is not allowed: a reference to a file is a path relative to the file it is in, as a scenario's $include path is",
                id="absolute",
            ),
        ],
    )
    def test_reference_path_rules(self, api, ref, reason):
        """The carrier passes httpchain_ref_parent_traversal_depth with the
        rootdir: the rules a scenario's $include path keeps."""
        scenario_dir = api / "a" / "b" / "c" / "d"
        message = self._failure({"$ref": ref}, {}, scenario_dir, ref_root=api)
        assert message == f"Cannot resolve a reference in inline body schema: $ref {ref!r} {reason}"

    def test_pointer_to_an_invalid_schema(self, api):
        message = self._failure("api/openapi.json#/components/schemas/Invalid", {}, api)
        assert message.startswith(f"Invalid JSON Schema in file '{api / 'api' / 'openapi.json'}#/components/schemas/Invalid': 12 is not valid under any of the given schemas")

    @pytest.mark.parametrize(
        ("component", "reason"),
        [
            pytest.param("Missing", "$ref 'missing.json' names {missing}, which does not exist", id="missing-file"),
            # Not referencing's own message, which quotes the whole document.
            pytest.param("Nowhere", "$ref '#/components/schemas/Nobody' points to nothing in {openapi}", id="pointer-to-nothing"),
            # What referencing lets through, int() refusing an index by a
            # name: in `validate --deep`'s words, not as an invalid schema.
            pytest.param("ByName", "$ref '#/components/schemas/Pair/prefixItems/first' cannot be resolved: invalid literal for int() with base 10: 'first'", id="array-by-a-name"),
        ],
    )
    def test_unresolvable_reference(self, api, component, reason):
        message = self._failure(f"api/openapi.json#/components/schemas/{component}", {}, api)
        openapi = api / "api" / "openapi.json"
        where = f"body schema file '{openapi}#/components/schemas/{component}'"
        assert message == f"Cannot resolve a reference in {where}: {reason.format(missing=api / 'api' / 'missing.json', openapi=openapi)}"

    def test_bundled_schema_resolves_by_its_ids(self, tmp_path):
        """The bundling form: a root `$id` is the base of the references
        inside it, so `/schemas/email` names the embedded resource, not an
        absolute file path to refuse."""
        schema = {
            "$id": "https://example.com/schemas/user",
            "type": "object",
            "properties": {"email": {"$ref": "/schemas/email"}},
            "$defs": {"email": {"$id": "/schemas/email", "type": "string", "format": "email"}},
        }
        self._verify(schema, {"email": "a@example.com"}, tmp_path, ref_root=tmp_path)
        assert self._failure(schema, {"email": "nope"}, tmp_path, ref_root=tmp_path).startswith("Body schema validation failed: 'nope' is not a 'email'")

    def test_reference_outside_the_root(self, api):
        """The carrier passes pytest's rootdir, which a scenario's $include
        must stay within too."""
        project = api / "api" / "project"
        project.mkdir()
        message = self._failure({"$ref": "../common.json"}, {}, project, ref_root=project)
        assert message == (
            f"Cannot resolve a reference in inline body schema: $ref '../common.json' names {api / 'api' / 'common.json'}, outside the reference root "
            f"{project.resolve()}: a schema's references must stay within it, as a scenario's $include must"
        )

    def test_remote_reference(self, tmp_path):
        message = self._failure({"$ref": "https://schemas.example.com/user.json"}, {}, tmp_path)
        assert message == (
            "Cannot resolve a reference in inline body schema: $ref 'https://schemas.example.com/user.json' names https://schemas.example.com/user.json, "
            "a remote document: remote references are not fetched, so keep it in a local file"
        )

    @pytest.mark.parametrize(
        ("referenced", "body", "reason"),
        [
            # Meta-checked when validating against it fails: each keyword
            # crashed on what its meta-schema refuses, in its own words
            # (jsonschema's UnknownType, re.error, a TypeError comparing).
            pytest.param({"type": "strin"}, 1, "'strin' is not valid under any of the given schemas", id="unknown-type"),
            pytest.param({"type": "string", "pattern": "("}, "x", "'(' is not a 'regex'", id="pattern-re-refuses"),
            pytest.param({"type": "integer", "minimum": "5"}, 1, "'5' is not of type 'number'", id="minimum-not-a-number"),
            # A reference that is not a string ('int' object has no attribute
            # 'partition'), a document that is not a schema ('list' object
            # has no attribute 'items').
            pytest.param({"$ref": 5}, 1, "5 is not of type 'string'", id="reference-not-a-string"),
            pytest.param({"$ref": None}, 1, "None is not of type 'string'", id="reference-null"),
            pytest.param([{"type": "integer"}], 1, "[{'type': 'integer'}] is not of type 'object', 'boolean'", id="document-a-list"),
            pytest.param(None, 1, "None is not of type 'object', 'boolean'", id="document-null"),
        ],
    )
    def test_invalid_schema_a_reference_reaches(self, tmp_path, referenced, body, reason):
        """Named by the reference, as `validate --deep` names it."""
        (tmp_path / "bad.json").write_text(json.dumps(referenced))
        message = self._failure({"$ref": "bad.json"}, body, tmp_path)
        assert message.startswith(f"Cannot validate against inline body schema: $ref 'bad.json' points to an invalid JSON Schema: {reason}\n")

    def test_invalid_schema_is_named_by_the_reference_nearest_to_it(self, tmp_path):
        """Through a component that references another, in the document the
        pointer selects it from: the reference to the invalid one, whatever
        references lead there."""
        components = {"A": {"properties": {"x": {"$ref": "#/components/schemas/B"}}}, "B": {"items": {"$ref": "#/components/schemas/C"}}, "C": {"$ref": ["#/components/schemas/B"]}}
        (tmp_path / "openapi.json").write_text(json.dumps({"openapi": "3.1.0", "components": {"schemas": components}}))
        message = self._failure("openapi.json#/components/schemas/A", {"x": [1]}, tmp_path)
        where = f"body schema file '{tmp_path / 'openapi.json'}#/components/schemas/A'"
        assert message.startswith(
            f"Cannot validate against {where}: $ref '#/components/schemas/C' points to an invalid JSON Schema: ['#/components/schemas/B'] is not of type 'string'\n"
        )

    def test_crash_nothing_accounts_for(self, tmp_path, monkeypatch):
        """Where the target's meta-check itself runs out of stack, it tells
        nothing: the keyword's crash is what the step reports."""
        (tmp_path / "list.json").write_text(json.dumps([{"type": "integer"}]))

        def too_deep(cls, schema, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        verify = Verify.model_validate({"body": {"schema": {"$ref": "list.json"}}})
        monkeypatch.setattr(jsonschema.Draft202012Validator, "check_schema", classmethod(too_deep))
        with pytest.raises(VerificationError) as excinfo:
            process_verify(verify, httpx.Response(200, json=1), tmp_path, ref_bounds=ReferenceBounds(None, 3))
        assert str(excinfo.value) == "Cannot validate against inline body schema, a schema it references is not valid: 'list' object has no attribute 'items'"

    def test_reference_that_is_not_a_string_where_the_dialect_allows_one(self, tmp_path):
        """Draft 4's meta-schema says nothing of `$ref`: an unresolvable
        reference, not a crash on it, in the words deep uses."""
        schema = {"$schema": "http://json-schema.org/draft-04/schema#", "properties": {"a": {"$ref": 5}}}
        (tmp_path / "d4.json").write_text(json.dumps(schema))
        message = self._failure("d4.json", {"a": 1}, tmp_path)
        assert message == f"Cannot resolve a reference in body schema file '{tmp_path / 'd4.json'}': $ref 5 is not a string: a reference is a URI reference"

    @pytest.mark.parametrize(
        ("document", "pointer", "reason"),
        [
            # urljoin's TypeError, "Cannot mix str and non-str arguments".
            pytest.param({"$id": 5, "$defs": {"a": {"type": "integer"}}}, "/$defs/a", "$id 5 is not a string: an $id is a URI reference", id="root-id-a-number"),
            # Draft 4 reads it with startswith(): "'dict' object has no
            # attribute 'startswith'".
            pytest.param(
                {"$schema": "http://json-schema.org/draft-04/schema#", "definitions": {"wrap": {"id": {"type": "string"}, "definitions": {"a": {"type": "integer"}}}}},
                "/definitions/wrap/definitions/a",
                'id {"type": "string"} is not a string: an id is a URI reference',
                id="draft-4-id-on-the-way-an-object",
            ),
        ],
    )
    def test_id_that_is_not_a_string_on_the_pointer_s_way(self, tmp_path, document, pointer, reason):
        """JSON Schema reads it before the schema, so no body passes, in the
        words `validate --deep` uses, which crashed on it."""
        (tmp_path / "doc.json").write_text(json.dumps(document))
        message = self._failure(f"doc.json#{pointer}", 1, tmp_path)
        assert message == f"Cannot resolve a reference in body schema file '{tmp_path / 'doc.json'}#{pointer}': {reason}"

    def test_template_text_is_refused(self):
        """A template can render to another template's text, which the
        field's template branch takes as it is: a file named '{{ y }}' was
        looked for."""
        message = str(_failure({"body": {"schema": "{{ y }}"}}))
        assert message == "verify.body.schema must resolve to a JSON Schema or a schema file, got '{{ y }}'"

    def test_listed_among_the_step_s_other_failures(self, api):
        message = self._failure("api/openapi.json#/components/schemas/Nobody", {}, api, status=201)
        assert message.split("\n") == [
            "2 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            f"  2. Body schema pointer '#/components/schemas/Nobody' leads nowhere in file '{api / 'api' / 'openapi.json'}': '#/components/schemas' has no key 'Nobody'",
        ]


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
            # The failure once quoted the value's repr, which a `vars` object
            # this deep overflowed, escaping as a bare RecursionError.
            pytest.param(convert_dict_to_namespace({"deep": nested("x", LOADABLE_BUT_DEEP)}), "VarsNamespace", id="vars-object-nested-hundreds-deep"),
        ],
    )
    def test_non_bool_is_rejected_naming_its_type(self, value, type_name):
        with pytest.raises(VerificationError, match=f"Verify expression 0 must evaluate to bool, got {type_name}"):
            process_verify(Verify(expressions=[value]), httpx.Response(200))

    def test_non_bool_failure_leaves_the_value_out(self):
        """The value can be a credential (`{{ response.headers['Set-Cookie'] }}`),
        which the report redacts among the headers: the failure names its
        type only, as skip_if's does."""
        with pytest.raises(VerificationError) as excinfo:
            process_verify(Verify(expressions=["sid=s3cr3t; Path=/"]), httpx.Response(200))
        assert str(excinfo.value) == "Verify expression 0 must evaluate to bool, got str, a value written where a condition belongs"


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
        # Nothing of it is hidden, so a pattern that is not its literal text
        # is still shown: it was once "[REDACTED]" for a value shown whole.
        pytest.param({"X-Id": {"not_matches": "^[0-9]$"}}, DEFAULT_REDACTION, "Header 'X-Id' (value: '7') matches '^[0-9]$' while it shouldn't", id="unlisted-header-not-matches"),
        pytest.param(
            {"Set-Cookie": {"not_matches": "sid=a.c"}},
            NO_REDACTION,
            "Header 'Set-Cookie' (value: 'sid=abc; Path=/, csrf=def; Path=/') matches 'sid=a.c' while it shouldn't",
            id="disabled-not-matches",
        ),
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


@pytest.mark.parametrize(
    ("header", "pattern", "shown", "error"),
    [
        pytest.param("X-Id", "{{ ( }}", "'{{ ( }}'", "missing ), unterminated subpattern at position 3", id="re-error"),
        pytest.param("X-Id", "a{4294967296}{{ x }}", "'a{4294967296}{{ x }}'", "the repetition number is too large", id="overflow"),
        # Its text is in the part of the value the redaction hides.
        pytest.param("Set-Cookie", "({{ x }}", "[REDACTED]", "missing ), unterminated subpattern at position 0", id="hidden-part"),
    ],
)
@pytest.mark.parametrize("key", ["matches", "not_matches"])
def test_header_pattern_re_refuses_fails_its_check(header, pattern, shown, error, key):
    """Template text a template rendered passes a header matcher's template
    branch, and ``re`` may refuse it (a pattern saved from a response): its
    check fails, and the step's other failures are still reported."""
    response = httpx.Response(200, headers=[("set-cookie", "sid=({{ x }}; Path=/"), ("x-id", "7")])
    with pytest.raises(VerificationError) as excinfo:
        process_verify(Verify.model_validate({"status": 201, "headers": {header: {key: pattern}}}), response)
    value = "'sid=[REDACTED]; Path=/'" if header == "Set-Cookie" else "'7'"
    assert str(excinfo.value).split("\n") == [
        "2 verification checks failed:",
        "  1. Status code doesn't match: expected 201, got 200",
        f"  2. Header '{header}' (value: {value}): {key} must resolve to a regular expression, got {shown} ({error})",
    ]


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
            # Every key must hold, each a check of its own: here gt alone fails.
            pytest.param({"count": {"lt": 10, "gt": 5}}, "JMESPath 'count' doesn't match: expected gt 5, got 3", id="all-keys-must-hold"),
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

    @pytest.mark.parametrize(
        ("pattern", "error"),
        [
            pytest.param("a{4294967296}{{ x }}", "the repetition number is too large", id="overflow"),
            pytest.param("(" * 5000 + "{{ x }}" + ")" * 5000, "maximum recursion depth exceeded", id="recursion"),
        ],
    )
    @pytest.mark.parametrize("key", ["matches", "not_matches"])
    def test_template_text_pattern_re_cannot_compile_fails_its_check(self, pattern, error, key):
        """For a repeat count or a nesting too big, re raises OverflowError or
        RecursionError, not re.error: raised out of the step, it took the step's
        other failures with it."""
        verify = {"status": 201, "jmespath": {"data.name": {key: pattern}}, "body": {"contains": ["zzz"]}}
        assert str(_failure(verify)).split("\n") == [
            "3 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            f"  2. JMESPath 'data.name': {key} must resolve to a regular expression, got {pattern!r} ({error})",
            "  3. Body doesn't contain 'zzz'",
        ]

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
            pytest.param(COMMENTED_JSON, id="comment"),
            pytest.param(TRAILING_COMMA_JSON, id="trailing-comma"),
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


HELPERS = "tests.unit.response_steps_test_helpers"


def _failure(verify: dict, response: httpx.Response = JSON_BODY) -> VerificationError:
    with pytest.raises(VerificationError) as excinfo:
        process_verify(Verify.model_validate(verify), response)
    return excinfo.value


class TestEveryFailureIsReported:
    """A verify step runs all its checks and reports every one that failed,
    where it stopped at the first: fixing a scenario took one run per wrong
    assertion. One failure reads as it always has; several are counted, then
    numbered in the order the checks ran."""

    def test_one_failure_among_passing_checks_reads_as_before(self):
        verify = {
            "status": 200,
            "headers": {"X-Id": "7", "Content-Type": {"contains": "json"}},
            "jmespath": {"count": 3, "data.id": 43, "data.name": {"type": "string"}},
            "expressions": [True],
            "body": {"schema": {"type": "object"}, "contains": ["Alice"]},
        }
        response = httpx.Response(200, json=BODY, headers={"x-id": "7"})
        assert str(_failure(verify, response)) == "JMESPath 'data.id' doesn't match: expected 43, got 42"

    def test_failures_are_listed_in_the_order_the_checks_ran(self):
        """status, headers, jmespath, expressions, user_functions, body.schema,
        body text. Each header, jmespath entry, expression, user function and
        body operand is a check of its own, run in the order written; so is
        each field a header matcher sets and each key a jmespath matcher sets,
        run in the order the model lists them, whatever the order written."""
        verify = Verify.model_validate(
            {
                "status": 200,
                "headers": {"X-Id": "8", "Content-Type": {"not_contains": "json", "contains": "xml"}},
                "jmespath": {"data.id": 43, "count": {"type": "string", "lt": 10, "gt": 5}, "data.name": "Alice"},
                "expressions": [True, False, "x"],
                "user_functions": [f"{HELPERS}:returns_false", f"{HELPERS}:raises"],
                "body": {"schema": {"type": "object", "required": ["missing"]}, "contains": ["Bob"], "not_matches": ["Alice"]},
            }
        )
        response = httpx.Response(500, json=BODY, headers={"x-id": "7"})
        with pytest.raises(VerificationError) as excinfo:
            process_verify(verify, response)
        lines = str(excinfo.value).split("\n")
        # jsonschema's own lines under its first (the schema, the instance)
        # are its to format: `test_later_lines_of_a_message_stay_under_its_number`.
        schema_lines = slice(lines.index("  12. Body schema validation failed: 'missing' is a required property") + 1, -2)
        del lines[schema_lines]
        assert lines == [
            "14 verification checks failed:",
            "  1. Status code doesn't match: expected 200, got 500",
            "  2. Header 'X-Id' doesn't match: expected 8, got 7",
            "  3. Header 'Content-Type' (value: 'application/json') doesn't contain 'xml'",
            "  4. Header 'Content-Type' (value: 'application/json') contains 'json' while it shouldn't",
            "  5. JMESPath 'data.id' doesn't match: expected 43, got 42",
            "  6. JMESPath 'count' doesn't match: expected gt 5, got 3",
            "  7. JMESPath 'count' doesn't match: expected type string, got 3 (number)",
            "  8. Expression 1 failed: evaluated to False",
            "  9. Verify expression 2 must evaluate to bool, got str, a value written where a condition belongs",
            # Among several, a function is named by its import name and index.
            f"  10. Function '{HELPERS}:returns_false' (user_functions[0]) verification failed",
            f"  11. Error calling user function '{HELPERS}:raises' (user_functions[1]): Error calling function '{HELPERS}:raises': boom",
            "  12. Body schema validation failed: 'missing' is a required property",
            "  13. Body doesn't contain 'Bob'",
            "  14. Body matches 'Alice' while it shouldn't",
        ]

    def test_later_lines_of_a_message_stay_under_its_number(self):
        """A message of several lines (jsonschema's) is indented under its own
        first line, so the next number still starts a line of its own."""
        message = str(_failure({"status": 201, "body": {"schema": {"type": "array"}}}))
        first, second, *rest = message.split("\n")
        assert (first, second) == ("2 verification checks failed:", "  1. Status code doesn't match: expected 201, got 200")
        assert rest[0].startswith("  2. Body schema validation failed: ")
        assert all(line == "" or line.startswith("     ") for line in rest[1:])

    def test_numbers_past_nine_keep_their_lines_aligned(self):
        """Numbering goes on unpadded, and a message's later lines sit under
        its own first line's text: one space further in from 10 on."""
        verify = {"expressions": [False] * 9, "body": {"schema": {"type": "array"}, "contains": ["j"]}}
        lines = str(_failure(verify, httpx.Response(200, json={}))).split("\n")
        assert lines[:2] == ["11 verification checks failed:", "  1. Expression 0 failed: evaluated to False"]
        assert lines[9:11] == ["  9. Expression 8 failed: evaluated to False", "  10. Body schema validation failed: {} is not of type 'array'"]
        assert lines[11:-1] == [
            "",
            "      Failed validating 'type' in schema:",
            "          {'type': 'array'}",
            "",
            "      On instance:",
            "          {}",
        ]
        assert lines[-1] == "  11. Body doesn't contain 'j'"

    def test_functions_are_told_apart_among_several(self):
        """A lone failure keeps the words it had, some of which name no
        function; in a list, each names its function and its index, so two
        calls of one function are two items that can be told apart."""
        verify = {"user_functions": [f"{HELPERS}:returns_none", {"name": f"{HELPERS}:returns_none", "kwargs": {}}]}
        assert str(_failure(verify)).split("\n") == [
            "2 verification checks failed:",
            f"  1. Function '{HELPERS}:returns_none' (user_functions[0]) must return bool, got NoneType",
            f"  2. Function '{HELPERS}:returns_none' (user_functions[1]) must return bool, got NoneType",
        ]
        assert str(_failure({"user_functions": [f"{HELPERS}:returns_none"]})) == "Verify function must return bool, got NoneType"

    @pytest.mark.parametrize(
        ("verify", "message"),
        [
            # One failure however many entries, and in the words of the check
            # that read the body first, as when it stopped the step.
            pytest.param({"jmespath": {"a": 1, "b": {"gt": 0}, "c": None}}, "Cannot check verify.jmespath", id="jmespath-entries"),
            pytest.param({"jmespath": {"a": 1}, "body": {"schema": {"type": "object"}}}, "Cannot check verify.jmespath", id="jmespath-and-schema"),
            pytest.param({"body": {"schema": {"type": "object"}}}, "Cannot validate schema", id="schema"),
        ],
    )
    def test_body_that_is_not_json_fails_once(self, verify, message):
        assert str(_failure(verify, NOT_JSON)).startswith(f"{message}, response is not valid JSON: ")

    def test_checks_not_reading_the_json_still_run_beside_an_unparsable_body(self):
        verify = {"status": 201, "jmespath": {"a": 1, "b": 2}, "body": {"schema": {"type": "object"}, "contains": ["nope"]}}
        lines = str(_failure(verify, NOT_JSON)).split("\n")
        assert lines[0] == "3 verification checks failed:"
        assert lines[1] == "  1. Status code doesn't match: expected 201, got 200"
        assert lines[2].startswith("  2. Cannot check verify.jmespath, response is not valid JSON: ")
        assert lines[3:] == ["  3. Body doesn't contain 'nope'"]

    def test_expression_that_cannot_be_evaluated_fails_once_for_its_matcher(self):
        """No key of its matcher can be judged: one failure, not one per key.
        The expressions after it still run."""
        verify = {"jmespath": {"length(count)": {"gt": 1, "lt": 0, "type": "string"}, "data.id": 43}}
        assert str(_failure(verify)).split("\n") == [
            "2 verification checks failed:",
            "  1. JMESPath 'length(count)' cannot be evaluated against the response body: length() needs string or array or object, got 3 (number)",
            "  2. JMESPath 'data.id' doesn't match: expected 43, got 42",
        ]

    def test_lone_failure_keeps_its_cause(self):
        error = _failure({"user_functions": [f"{HELPERS}:raises"]})
        assert isinstance(error.__cause__, UserFunctionError)

    @pytest.mark.parametrize(
        ("function", "outcome"),
        [
            pytest.param("skips", pytest.skip.Exception, id="skip"),
            pytest.param("xfails", pytest.xfail.Exception, id="xfail"),
            pytest.param("fails", pytest.fail.Exception, id="fail"),
        ],
    )
    def test_user_function_outcome_ends_a_step_that_has_not_failed(self, function, outcome):
        """As before: the outcome is the stage's, and the checks after the
        function (the body's) do not run."""
        verify = Verify.model_validate({"user_functions": [f"{HELPERS}:{function}"], "body": {"contains": ["nope"]}})
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            process_verify(verify, JSON_BODY)
        assert excinfo.type is outcome

    @pytest.mark.parametrize(
        ("function", "listed"),
        [
            # The function ran only because the status failure no longer
            # stopped the step: its skip or xfail must not turn the failed
            # stage into a skipped or xfailed one.
            pytest.param("skips", [], id="skip"),
            pytest.param("xfails", [], id="xfail"),
            pytest.param("fails", [f"Function '{HELPERS}:fails' (user_functions[0]) called pytest.fail(): custom check failed"], id="fail"),
        ],
    )
    def test_user_function_outcome_cannot_override_an_earlier_failure(self, function, listed):
        """The outcome still ends the step: the body's check after it does not run."""
        verify = {"status": 201, "user_functions": [f"{HELPERS}:{function}"], "body": {"contains": ["nope"]}}
        status_failure = "Status code doesn't match: expected 201, got 200"
        expected = [status_failure] if not listed else ["2 verification checks failed:", f"  1. {status_failure}", f"  2. {listed[0]}"]
        assert str(_failure(verify)).split("\n") == expected


def _renderer(rendered: dict | None = None, asked: list | None = None):
    """A `VerifyRender` giving each value as declared, but ``rendered[where]``
    where it has one (a `RenderFailure`, a `RenderOutcome`), and leaving out
    the values after an outcome, as the carrier's does. ``asked`` notes each
    call's values, by where."""

    def render(values):
        if asked is not None:
            asked.append([where for where, _value in values])
        result = {}
        for where, value in values:
            result[where] = (rendered or {}).get(where, value)
            if isinstance(result[where], RenderOutcome):
                break
        return result

    return render


def _cannot_render(message: str) -> RenderFailure:
    return RenderFailure(VerificationError(message))


def _outcome(raise_it) -> RenderOutcome:
    """The outcome ``raise_it()`` raises, as a template raised it."""
    try:
        raise_it()
    except (pytest.skip.Exception, pytest.fail.Exception) as e:
        return RenderOutcome(e)
    raise AssertionError("raised no outcome")


def _shown_chain(error: BaseException | None):
    """The exceptions a report shows for ``error``: it, then what it was
    raised from, or while handling unless that is suppressed."""
    while error is not None:
        yield error
        error = error.__cause__ if error.__cause__ is not None or error.__suppress_context__ else error.__context__


class TestRender:
    """``render`` renders the step's values (the carrier renders templates),
    once, before the first check runs, given each where it is declared: a
    value that cannot be rendered is one failure in its check's place, and
    that check does not run, where rendering the step whole ended it at the
    first."""

    def test_is_given_each_value_where_declared_in_check_order(self):
        verify = Verify.model_validate(
            {
                "description": "d",
                "status": 200,
                "headers": {"A": "1", "B": {"contains": "x"}},
                "jmespath": {"data.id": 42, "count": {"gt": 1}},
                "expressions": [True, True],
                "user_functions": [f"{HELPERS}:returns_true"],
                "body": {"schema": {"type": "object"}, "contains": ["data"], "not_contains": ["zzz"], "matches": ["."], "not_matches": ["zzz"]},
            }
        )
        asked = []
        process_verify(verify, httpx.Response(200, json=BODY, headers={"a": "1", "b": "x"}), render=_renderer(asked=asked))
        assert asked == [
            [
                ("description",),
                ("status",),
                ("headers", "A"),
                ("headers", "B"),
                ("jmespath", "data.id"),
                ("jmespath", "count"),
                ("expressions", 0),
                ("expressions", 1),
                ("user_functions", 0),
                ("body", "schema"),
                ("body", "contains", 0),
                ("body", "not_contains", 0),
                ("body", "matches", 0),
                ("body", "not_matches", 0),
            ]
        ]

    def test_value_that_does_not_render_is_one_failure_and_its_check_does_not_run(self):
        unrenderable = [("headers", "A"), ("user_functions", 0), ("body", "contains", 1)]
        render = _renderer({where: _cannot_render(f"cannot render {where}") for where in unrenderable})
        # The function would raise: not called.
        verify = Verify.model_validate({"status": 201, "headers": {"A": "1"}, "user_functions": [f"{HELPERS}:raises"], "body": {"contains": ["zzz", "yyy", "www"]}})
        with pytest.raises(VerificationError) as excinfo:
            process_verify(verify, JSON_BODY, render=render)
        assert str(excinfo.value).split("\n") == [
            "6 verification checks failed:",
            "  1. Status code doesn't match: expected 201, got 200",
            "  2. cannot render ('headers', 'A')",
            "  3. cannot render ('user_functions', 0)",
            "  4. Body doesn't contain 'zzz'",
            "  5. cannot render ('body', 'contains', 1)",
            "  6. Body doesn't contain 'www'",
        ]

    def test_jmespath_entry_that_does_not_render_leaves_the_body_to_the_next(self):
        """The body is parsed by the first entry that renders: one that is not
        JSON is still one failure, after the entries that did not render."""
        render = _renderer({("jmespath", "a"): _cannot_render("cannot render a")})
        with pytest.raises(VerificationError) as excinfo:
            process_verify(Verify.model_validate({"jmespath": {"a": 1, "b": 2, "c": 3}}), NOT_JSON, render=render)
        first, cannot_render, not_json = str(excinfo.value).split("\n")
        assert (first, cannot_render) == ("2 verification checks failed:", "  1. cannot render a")
        assert not_json.startswith("  2. Cannot check verify.jmespath, response is not valid JSON: ")

    def test_every_value_renders_before_the_first_check(self, monkeypatch):
        """In one call, before the function (a check) is called."""
        events = []
        monkeypatch.setattr(response_steps_test_helpers, "EVENTS", events)
        verify = Verify.model_validate({"status": 200, "user_functions": [f"{HELPERS}:records"], "body": {"contains": ["data"]}})
        process_verify(verify, JSON_BODY, render=_renderer(asked=events))
        assert events == [[("status",), ("user_functions", 0), ("body", "contains", 0)], "called"]

    @pytest.mark.parametrize(
        ("function", "listed"),
        [
            pytest.param("skips", [], id="skip"),
            pytest.param("xfails", [], id="xfail"),
            pytest.param("fails", [f"Function '{HELPERS}:fails' (user_functions[0]) called pytest.fail(): custom check failed"], id="fail"),
        ],
    )
    def test_function_outcome_cannot_hide_a_value_after_it_that_did_not_render(self, function, listed):
        """The step still ends at the function: the body operand that did
        render is not checked. The one that did not is a failure of the step
        wherever it sits, and a skip or xfail must not make a stage whose
        template failed pass as skipped."""
        render = _renderer({("body", "contains", 1): _cannot_render("cannot render the operand")})
        verify = Verify.model_validate({"user_functions": [f"{HELPERS}:{function}"], "body": {"contains": ["nope", "{{ x }}"]}})
        with pytest.raises(VerificationError) as excinfo:
            process_verify(verify, JSON_BODY, render=render)
        expected = ["cannot render the operand"] if not listed else ["2 verification checks failed:", f"  1. {listed[0]}", "  2. cannot render the operand"]
        assert str(excinfo.value).split("\n") == expected

    TEMPLATE_FAILED = "The template at 'verify.body.contains[1]' called pytest.fail(): template failed"

    @pytest.mark.parametrize(
        ("function", "status", "expected"),
        [
            # The template's fail() is the step's one failure: raised as
            # itself, as when the step rendered whole before any check.
            pytest.param("skips", 200, None, id="skip"),
            pytest.param("xfails", 200, None, id="xfail"),
            pytest.param(
                "fails",
                200,
                ["2 verification checks failed:", f"  1. Function '{HELPERS}:fails' (user_functions[0]) called pytest.fail(): custom check failed", f"  2. {TEMPLATE_FAILED}"],
                id="fail",
            ),
            pytest.param(
                "skips",
                201,
                ["2 verification checks failed:", "  1. Status code doesn't match: expected 201, got 200", f"  2. {TEMPLATE_FAILED}"],
                id="skip-after-a-failure",
            ),
        ],
    )
    def test_function_outcome_cannot_hide_a_template_fail_after_it(self, function, status, expected):
        """A template after the function that called pytest.fail() failed the
        stage when the step rendered whole: a skip or xfail must not turn that
        into a skipped or xfailed stage either."""
        render = _renderer({("body", "contains", 1): _outcome(lambda: pytest.fail("template failed"))})
        verify = Verify.model_validate({"status": status, "user_functions": [f"{HELPERS}:{function}"], "body": {"contains": ["nope", "{{ end() }}"]}})
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception, VerificationError)) as excinfo:
            process_verify(verify, JSON_BODY, render=render)
        if expected is None:
            assert (excinfo.type, str(excinfo.value)) == (pytest.fail.Exception, "template failed")
        else:
            assert (excinfo.type, str(excinfo.value).split("\n")) == (VerificationError, expected)
        # Raised while the function's outcome was handled, the failure must not
        # show it: a report of a failed stage opened with "Skipped: ...".
        assert list(_shown_chain(excinfo.value)) == [excinfo.value]

    @pytest.mark.parametrize("later", [pytest.skip, pytest.xfail], ids=["skip", "xfail"])
    def test_template_skip_after_a_function_outcome_is_dropped(self, later):
        """It is no failure: the function's own outcome ends the step."""
        render = _renderer({("body", "contains", 0): _outcome(lambda: later("later"))})
        verify = Verify.model_validate({"user_functions": [f"{HELPERS}:skips"], "body": {"contains": ["{{ end() }}"]}})
        with pytest.raises((pytest.skip.Exception, pytest.fail.Exception)) as excinfo:
            process_verify(verify, JSON_BODY, render=render)
        assert (excinfo.type, str(excinfo.value)) == (pytest.skip.Exception, "not on this server")

    @pytest.mark.parametrize(
        ("failing", "outcome"),
        [
            pytest.param({}, pytest.skip.Exception, id="ends-a-step-that-has-not-failed"),
            pytest.param({("headers", "A"): _cannot_render("cannot render A")}, VerificationError, id="earlier-failure-wins"),
        ],
    )
    def test_outcome_raised_while_rendering_ends_the_step_there(self, failing, outcome):
        """When the checks reach it. The values after it, which the rendering
        left out, are not asked for."""
        render = _renderer({**failing, ("expressions", 0): _outcome(lambda: pytest.skip("not here"))})
        verify = Verify.model_validate({"headers": {"A": "1"}, "expressions": ["{{ x }}", "{{ y }}"], "body": {"contains": ["{{ z }}"]}})
        # Both caught, so a wrong outcome fails this test rather than skipping it.
        with pytest.raises((pytest.skip.Exception, VerificationError)) as excinfo:
            process_verify(verify, httpx.Response(200, headers={"a": "1"}), render=render)
        assert excinfo.type is outcome

    def test_lone_render_failure_keeps_its_message_and_cause(self):
        cause = TemplatesError("KeyError in expression")
        error = VerificationError("KeyError in expression")
        error.__cause__ = cause
        with pytest.raises(VerificationError) as excinfo:
            process_verify(Verify(status=200), httpx.Response(200), render=_renderer({("status",): RenderFailure(error)}))
        assert str(excinfo.value) == "KeyError in expression"
        assert excinfo.value.__cause__ is cause


class TestRetryable:
    """Whether a stage's ``retry`` may attempt the stage again after the
    step's failure: after its checks failed on this response, not after one
    of the scenario's own failures, which the next attempt would repeat (a
    value that did not render or rendered one its check cannot take, a body
    schema that cannot be read, a user function that cannot be called or
    crashed), nor after a function ended it with pytest.fail()."""

    # Each schema file a row names, in the scenario's directory.
    SCHEMA_FILES = {"job.json": {"components": {"Job": {"type": "object", "required": ["id"]}}}, "invalid.json": {"type": "strin"}}

    @pytest.mark.parametrize(
        ("verify", "rendered", "retryable"),
        [
            pytest.param({"status": 201}, {}, True, id="check"),
            pytest.param({"user_functions": [f"{HELPERS}:returns_false"]}, {}, True, id="function-returned-false"),
            # A function's result on this response, however wrong its type.
            pytest.param({"user_functions": [f"{HELPERS}:returns_none"]}, {}, True, id="function-returned-non-bool"),
            # What a function raises to say "not yet", as a failed check does.
            pytest.param({"user_functions": [f"{HELPERS}:not_done_yet"]}, {}, True, id="function-raised-verification-error"),
            pytest.param({"user_functions": [f"{HELPERS}:not_done_ever"]}, {}, False, id="function-raised-unretryable-verification-error"),
            # A crash, and a function that cannot be called: every attempt alike.
            pytest.param({"user_functions": [f"{HELPERS}:raises"]}, {}, False, id="function-crashed"),
            pytest.param({"user_functions": [f"{HELPERS}:no_such_function"]}, {}, False, id="function-not-found"),
            pytest.param({"user_functions": ["no_such_module_xyz:check"]}, {}, False, id="module-not-importable"),
            pytest.param({"user_functions": [f"{HELPERS}:EVENTS"]}, {}, False, id="function-not-callable"),
            # One unretryable failure is enough, among checks that failed.
            pytest.param({"status": 201, "user_functions": [f"{HELPERS}:raises"]}, {}, False, id="function-crashed-among-check-failures"),
            pytest.param({"status": 200}, {("status",): _cannot_render("cannot render status")}, False, id="value-did-not-render"),
            pytest.param({"status": 201, "headers": {"A": "1"}}, {("headers", "A"): _cannot_render("cannot render A")}, False, id="value-did-not-render-among-check-failures"),
            # Rendered to template text, which its check cannot take.
            pytest.param({"status": "{{ x }}"}, {}, False, id="status-must-resolve"),
            pytest.param({"headers": {"A": {"matches": "{{ ( }}"}}}, {}, False, id="header-pattern-must-resolve"),
            pytest.param({"body": {"not_matches": ["{{ p }}"]}}, {("body", "not_matches", 0): "("}, False, id="body-pattern-must-resolve"),
            pytest.param({"jmespath": {"n": {"gt": "{{ x }}"}}}, {}, False, id="matcher-operand-must-resolve"),
            pytest.param({"status": 201, "jmespath": {"n": {"length": "{{ x }}"}}}, {}, False, id="matcher-operand-must-resolve-among-check-failures"),
            pytest.param({"body": {"schema": "{{ x }}"}}, {}, False, id="schema-must-resolve"),
            # Listed with the failure before it, not raised as itself.
            pytest.param({"status": 201, "expressions": ["{{ x }}"]}, {("expressions", 0): _outcome(lambda: pytest.fail("no"))}, False, id="template-called-fail"),
            pytest.param({"status": 201, "user_functions": [f"{HELPERS}:fails"]}, {}, False, id="function-called-fail"),
            # The body failing its schema is this response's; the schema
            # itself failing to be read is the scenario's.
            pytest.param({"body": {"schema": "job.json#/components/Job"}}, {}, True, id="body-fails-schema"),
            pytest.param({"body": {"schema": "missing.json"}}, {}, False, id="schema-file-missing"),
            pytest.param({"body": {"schema": "job.json#/components/Nobody"}}, {}, False, id="schema-pointer-leads-nowhere"),
            pytest.param({"body": {"schema": "invalid.json"}}, {}, False, id="schema-file-invalid"),
            pytest.param({"body": {"schema": {"$ref": "missing.json"}}}, {}, False, id="schema-reference-unresolvable"),
            pytest.param({"body": {"schema": {"$ref": "invalid.json"}}}, {}, False, id="schema-reference-invalid"),
        ],
    )
    def test_verify(self, tmp_path, verify, rendered, retryable):
        for name, schema in self.SCHEMA_FILES.items():
            (tmp_path / name).write_text(json.dumps(schema))
        with pytest.raises(VerificationError) as excinfo:
            process_verify(Verify.model_validate(verify), httpx.Response(200, json={"n": 1}), scenario_dir=tmp_path, render=_renderer(rendered))
        assert excinfo.value.retryable is retryable

    @pytest.mark.parametrize(
        ("save", "response", "retryable"),
        [
            pytest.param(JMESPathSave(jmespath={"id": "id"}), httpx.Response(502, text="<html>Bad Gateway</html>"), True, id="body-not-json"),
            pytest.param(JMESPathSave(jmespath={"n": "length(id)"}), httpx.Response(200, json={"id": 5}), True, id="expression-fails-on-this-body"),
            pytest.param(RegexSave(regex={"token": "token=(\\w+)"}), httpx.Response(200, text="pending"), True, id="regex-does-not-match"),
            # Rendered to what the step cannot use, as a value that does not validate.
            pytest.param(RegexSave.model_validate({"regex": {"token": "{{ ( }}"}}), httpx.Response(200, text="pending"), False, id="pattern-must-resolve"),
            pytest.param(RegexSave.model_validate({"regex": {"token": {"pattern": "(a)", "group": "{{ y }}"}}}), httpx.Response(200, text="a"), False, id="group-must-resolve"),
            pytest.param(RegexSave.model_validate({"regex": {"token": {"pattern": "(a)", "all": "{{ y }}"}}}), httpx.Response(200, text="a"), False, id="all-must-resolve"),
            pytest.param(RegexSave.model_validate({"regex": {"token": {"pattern": "{{ '(a)' }}", "group": 2}}}), httpx.Response(200, text="a"), False, id="group-not-in-pattern"),
            pytest.param(
                SubstitutionsSave.model_validate({"substitutions": [{"vars": {"x": "{{ missing }}"}}]}), httpx.Response(200, json={}), False, id="substitution-did-not-render"
            ),
            pytest.param(UserFunctionsSave(user_functions=[f"{HELPERS}:not_saved_yet"]), httpx.Response(200, json={}), True, id="function-raised-save-error"),
            pytest.param(UserFunctionsSave(user_functions=[f"{HELPERS}:returns_none"]), httpx.Response(200, json={}), True, id="function-returned-non-dict"),
            pytest.param(UserFunctionsSave(user_functions=[f"{HELPERS}:raises"]), httpx.Response(200, json={}), False, id="function-crashed"),
            pytest.param(UserFunctionsSave(user_functions=[f"{HELPERS}:no_such_function"]), httpx.Response(200, json={}), False, id="function-not-found"),
            pytest.param(UserFunctionsSave(user_functions=["no_such_module_xyz:extract"]), httpx.Response(200, json={}), False, id="module-not-importable"),
        ],
    )
    def test_save(self, save, response, retryable):
        with pytest.raises(SaveError) as excinfo:
            process_save(save, response, ChainMap())
        assert excinfo.value.retryable is retryable


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
