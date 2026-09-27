"""response_steps: the meaning of one verify or save step.

The passing side of each step type runs end to end in
tests/integration/test_verify.py and test_save.py; this pins the failure
messages and the edge cases a mock server cannot produce cheaply.
"""

import functools
import json
from collections import ChainMap

import httpx
import pytest

from pytest_httpchain.errors import SaveError, VerificationError
from pytest_httpchain.models import JMESPathSave, Verify
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.response_steps import check_rendered_assertions, process_save, process_verify
from tests.unit.helpers import TOO_DEEP_TO_PARSE, TOO_DEEP_TO_WALK

NOT_JSON = httpx.Response(200, content=b"not json", headers={"content-type": "text/plain"})
# The decoder raises RecursionError, which is not a ValueError: a narrower except
# let it escape the chain-abort machinery, with no report section and no HAR entry.
TOO_DEEP_JSON = httpx.Response(200, content=TOO_DEEP_TO_PARSE, headers={"content-type": "application/json"})
UNPARSEABLE = pytest.mark.parametrize("response", [NOT_JSON, TOO_DEEP_JSON], ids=["not-json", "too-deep"])


@UNPARSEABLE
def test_jmespath_save_rejects_non_json_response(response):
    with pytest.raises(SaveError, match="response is not valid JSON"):
        process_save(JMESPathSave(jmespath={"value": "key"}), response, ChainMap())


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
            TOO_DEEP_TO_PARSE,
        ],
        ids=["missing", "non-utf8", "too-deep"],
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

    @UNPARSEABLE
    def test_non_json_response_fails_cleanly(self, response):
        with pytest.raises(VerificationError, match="response is not valid JSON"):
            process_verify(Verify(body=ResponseBody(schema={"type": "object"})), response)

    @pytest.mark.parametrize(
        ("schema", "match"),
        [
            # Fails at the root, but the error's str() pretty-prints the whole
            # body, inside the except clause.
            pytest.param({"type": "object"}, "Body schema validation failed: .* is not of type 'object'", id="too-deep-to-describe"),
            # A self-referencing schema follows the body all the way down.
            pytest.param({"type": "array", "items": {"$ref": "#"}}, "response or schema is nested too deeply", id="too-deep-to-validate"),
        ],
    )
    def test_body_too_deep_to_validate_fails_cleanly(self, schema, match):
        """Parsing survives this depth; the Python code walking the result does not."""
        response = httpx.Response(200, content=TOO_DEEP_TO_WALK, headers={"content-type": "application/json"})
        with pytest.raises(VerificationError, match=match):
            process_verify(Verify(body=ResponseBody(schema=schema)), response)


class TestRenderedAwayAssertions:
    """A declared assertion that a template rendered to None must fail loudly:
    both models re-validate cleanly, so otherwise it is silently dropped and a
    500 passes green."""

    @pytest.mark.parametrize(
        ("declared", "match"),
        [
            (Verify(status="{{ expected }}"), "'status'.*rendered to None"),
            (Verify(body=ResponseBody(schema="{{ schema_path }}")), "body.schema.*rendered to None"),
        ],
        ids=["status", "body-schema"],
    )
    def test_rendered_away_assertion_is_rejected(self, declared, match):
        with pytest.raises(VerificationError, match=match):
            check_rendered_assertions(declared, Verify())

    @pytest.mark.parametrize(
        ("declared", "rendered"),
        [(Verify(), Verify()), (Verify(status="{{ expected }}"), Verify(status=200))],
        ids=["undeclared", "survived"],
    )
    def test_live_assertions_are_not_flagged(self, declared, rendered):
        check_rendered_assertions(declared, rendered)


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
            # Why `expressions` needs no check_rendered_assertions entry:
            # substitution rewrites the list element-wise, so a rendered-away
            # entry arrives here as None and fails the bool contract.
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
