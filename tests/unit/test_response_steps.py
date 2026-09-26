"""response_steps: the meaning of one verify or save step.

The passing side of each step type runs end to end in
tests/integration/test_verify.py and test_save.py; this pins the failure
messages and the edge cases a mock server cannot produce cheaply.
"""

import json
from collections import ChainMap

import httpx
import pytest

from pytest_httpchain.errors import SaveError, VerificationError
from pytest_httpchain.models import JMESPathSave, Verify
from pytest_httpchain.models.entities import ResponseBody
from pytest_httpchain.response_steps import check_rendered_assertions, process_save, process_verify

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
