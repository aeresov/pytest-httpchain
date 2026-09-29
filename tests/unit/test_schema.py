"""Unit tests for `schema.build_schema`, the editor JSON Schema for scenario files."""

import json
import re
from pathlib import Path

import jsonschema
import pytest

from pytest_httpchain.schema import build_schema
from pytest_httpchain.templates import TEMPLATE_PATTERN_ECMA, is_complete_template

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "schema" / "scenario.schema.json"

_STAGE = {"name": "s", "request": {"url": "https://x.test/"}}


def _with_stage(**fields) -> dict:
    return {"stages": [{**_STAGE, **fields}]}


def _request(**fields) -> dict:
    return _with_stage(request={"url": "https://x.test/", **fields})


def _verify(**fields) -> dict:
    return _with_stage(response=[{"verify": fields}])


@pytest.fixture(scope="module")
def validator():
    return jsonschema.Draft202012Validator(build_schema())


def test_build_schema_matches_committed():
    assert build_schema() == json.loads(SCHEMA_PATH.read_text())


def test_schema_metadata_is_hoisted_out_of_jsonref_branches():
    """Widening a subschema MOVES its title/description onto the `anyOf`
    wrapper. Copying them instead leaves an editor showing the same text twice
    at the sites that copy and once at the sites that move."""
    schema = build_schema()
    offenders: list[str] = []

    def collect(node, path):
        match node:
            case dict():
                match node.get("anyOf"):
                    case [{"$ref": "#/$defs/JsonRef"}, dict() as branch, *_]:
                        offenders.extend(f"{path}.anyOf[1].{key}" for key in ("title", "description") if key in branch)
                for key, value in node.items():
                    collect(value, f"{path}.{key}")
            case list():
                for index, item in enumerate(node):
                    collect(item, f"{path}[{index}]")

    collect(schema, "$")
    assert not offenders, f"metadata left inside JsonRef anyOf branches: {offenders}"

    # MOVED, not dropped: asserting only the absence above would also pass if the
    # hoist were deleted, silently stripping every root property and $defs entry
    # of the hover text the published editor schema exists to provide.
    assert schema["properties"]["stages"]["description"], "root property lost its description"
    assert schema["$defs"]["Stage"]["title"], "$defs entry lost its title"


def test_schema_descriptions_carry_no_rst_markup():
    """A model's class docstring becomes its schema description, which an
    editor shows on hover as plain text: RST's ``literal`` backticks, at home
    in the code's other docstrings, would show there as they are typed."""
    offenders: list[str] = []

    def collect(node, path):
        match node:
            case dict():
                if isinstance(node.get("description"), str) and "``" in node["description"]:
                    offenders.append(path)
                for key, value in node.items():
                    collect(value, f"{path}.{key}")
            case list():
                for index, item in enumerate(node):
                    collect(item, f"{path}[{index}]")

    collect(build_schema(), "$")
    assert not offenders, f"RST double backticks in schema descriptions: {offenders}"


def _schema_patterns(schema) -> list[str]:
    """Every `pattern` the schema constrains a string with. Examples and
    defaults are instances, not schemas: a ``save.regex`` example's
    ``pattern`` is a Python regex."""
    patterns: list[str] = []

    def collect(node):
        match node:
            case dict():
                for key, value in node.items():
                    if key == "pattern" and isinstance(value, str):
                        patterns.append(value)
                    elif key not in ("examples", "default"):
                        collect(value)
            case list():
                for item in node:
                    collect(item)

    collect(schema)
    return patterns


def test_schema_patterns_are_ecma262_compatible():
    """JSON Schema defines `pattern` as an ECMA-262 regex. Python's named-group
    spelling `(?P<name>...)` is a SyntaxError in JS engines, and VS Code's JSON
    language service silently drops a pattern it cannot compile — so no emitted
    pattern may use Python-only syntax."""
    patterns = _schema_patterns(build_schema())
    assert patterns, "expected the schema to carry pattern constraints"
    offenders = [p for p in patterns if "(?P<" in p]
    assert not offenders, f"Python-only named groups in schema patterns: {offenders}"


@pytest.mark.parametrize(
    "document",
    [
        pytest.param({"$schema": "https://aeresov.github.io/pytest-httpchain/schema/scenario.schema.json", "stages": []}, id="schema-key"),
        pytest.param({"$include": "base.json"}, id="root-include"),
        pytest.param(_with_stage(request={"$include": "common.json"}), id="request-include"),
        pytest.param({"stages": [{"$ref": "stage.json", "name": "override"}]}, id="stage-ref-with-override"),
        # Reference objects at tagged-union positions: these are anyOf in the
        # emitted schema; under oneOf a reference matched the JsonRef branch of
        # every member and was rejected as ambiguous.
        pytest.param(_request(body={"$include": "body.json"}), id="union-body"),
        pytest.param(_with_stage(response=[{"save": {"$merge": "save.json"}}]), id="union-save"),
        pytest.param(_with_stage(response=[{"$include": "step.json"}]), id="union-response-step"),
        pytest.param(_with_stage(substitutions=[{"$ref": "vars.json"}]), id="union-substitution"),
        pytest.param(_with_stage(parallel={"$include": "parallel.json"}), id="union-parallel"),
        pytest.param(_with_stage(parametrize=[{"$include": "params.json"}]), id="union-parametrize"),
        # Reference objects in place of a whole model field, not only of a
        # named type: shared checks, a shared step list, a shared base URL.
        pytest.param(_with_stage(response={"$include": "common.json#/checks"}), id="field-response"),
        pytest.param(_with_stage(substitutions={"$include": "common.json#/vars"}), id="field-substitutions"),
        pytest.param(_request(url={"$include": "common.json#/base_url"}), id="field-scalar"),
        pytest.param(_verify(expressions={"$merge": "common.json#/expressions"}), id="field-nested-model"),
        # Template-accepting fields take templates, concrete values, and the
        # stringified concretes the runtime coerces (no false positives).
        pytest.param(_request(timeout="{{ t }}"), id="timeout-template"),
        pytest.param(_request(timeout=30), id="timeout-concrete"),
        pytest.param(_request(timeout="30"), id="timeout-stringified"),
        pytest.param(_request(method="{{ m }}"), id="method-template"),
        pytest.param(_request(method="GET"), id="method-concrete"),
        # Any RFC 9110 token is a legal method since the widening.
        pytest.param(_request(method="FOOBAR"), id="method-token"),
        pytest.param(_verify(status="{{ s }}"), id="status-template"),
        pytest.param(_verify(status=200), id="status-concrete"),
        pytest.param(_verify(status="200"), id="status-stringified"),
        pytest.param(_verify(status="2xx"), id="status-class"),
        pytest.param(_verify(status="2XX"), id="status-class-uppercase"),
        pytest.param(_verify(status=[200, 201]), id="status-list"),
        pytest.param(_verify(status=["2xx", 304, "{{ s }}"]), id="status-list-mixed"),
        pytest.param(
            _verify(jmespath={"a": 1, "b": [{"id": 1}], "c": None, "d": "{{ x }}", "e": {"eq": {"page": 1}}, "f": {"ne": None, "gt": 0, "type": "{{ t }}"}}),
            id="jmespath-values-and-matchers",
        ),
        pytest.param(_verify(jmespath={"a": {"$include": "common.json#/expected"}}), id="jmespath-include"),
    ],
)
def test_schema_accepts_documented_shapes(validator, document):
    validator.validate(document)


@pytest.mark.parametrize(
    "document",
    [
        # Misspelled keys must not slip through the JsonRef branch.
        pytest.param({"stages": [{"naem": "s", "requst": {"url": "https://x.test/"}}]}, id="stage-keys"),
        pytest.param(_request(headerz={}), id="request-key"),
        # An otherwise-complete stage, so additionalProperties on Stage (not a
        # missing required "request") is what rejects it.
        pytest.param(_with_stage(alwaysrun=True), id="stage-key"),
        pytest.param(_with_stage(response=[{"save": {"jmespth": {"x": "y"}}}]), id="save-key"),
        pytest.param({"stagez": []}, id="root-key"),
        # A field's JsonRef branch still demands a directive key.
        pytest.param(_with_stage(response={"checks": "not a step"}), id="field-without-directive"),
        # L7: a template-accepting field rejects a non-template string that is
        # not a valid value of the field's concrete type either.
        pytest.param(_request(timeout="abc"), id="timeout-type"),
        pytest.param(_request(method="FOO BAR"), id="method-not-a-token"),
        pytest.param(_verify(status="not-a-status"), id="status-type"),
        pytest.param(_verify(status="6xx"), id="status-class-unknown"),
        pytest.param(_verify(status=[]), id="status-list-empty"),
        pytest.param(_verify(status=[200, "abc"]), id="status-list-entry-type"),
        # An object is a matcher: a literal one for equality belongs under eq.
        pytest.param(_verify(jmespath={"meta": {"page": 1}}), id="jmespath-literal-object"),
        pytest.param(_verify(jmespath={"meta": {}}), id="jmespath-empty-matcher"),
        pytest.param(_verify(jmespath={"price": {"gt": None}}), id="jmespath-null-bound"),
        pytest.param(_verify(jmespath={"price": {"gt": "5"}}), id="jmespath-numeric-text"),
        pytest.param(_verify(jmespath={"price": {"type": "int"}}), id="jmespath-type-unknown"),
        # An escaped template is text, which a complete-template field refuses
        # as the model does: the value would never be the number or switch.
        pytest.param(_request(timeout=r"\{{ t }}"), id="timeout-escaped"),
        pytest.param(_request(timeout=r"\\{{ t }}"), id="timeout-backslash-then-template"),
        pytest.param(_with_stage(skip_if=r"\{{ flag }}"), id="skip-if-escaped"),
    ],
)
def test_schema_rejects_typos(validator, document):
    assert not validator.is_valid(document)


@pytest.mark.parametrize(
    ("value", "complete"),
    [
        ("{{ t }}", True),
        ("  {{ t }}  ", True),
        (r"\{{ t }}", False),
        (r"\\{{ t }}", False),
        (r" \{{ t }}", False),
        # Braces in escaped text are text too.
        (r"\{{{{ t }}", False),
        ("{{ a }} {{ b }}", False),
        # The expression form is a template, a `{{` in its expression included.
        ("{{ '{{' }}", True),
    ],
)
def test_complete_template_pattern_agrees_with_the_engine(value, complete):
    """The schema's complete-template `pattern` (ECMA-262, no escape handling
    of its own) is anchored after whitespace, where no backslash can escape
    the braces: it accepts exactly what the engine takes whole, escaped forms
    never."""
    pattern = rf"^\s*{TEMPLATE_PATTERN_ECMA}\s*$"
    assert pattern in _schema_patterns(build_schema())
    assert (re.search(pattern, value) is not None) is complete
    assert is_complete_template(value) is complete
