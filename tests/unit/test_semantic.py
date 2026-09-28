"""Unit tests for `validation.check_scenario` (validation/semantic.py) on
in-memory scenarios. The file-based, end-to-end validator tests live in
test_validation.py."""

import pytest

from pytest_httpchain.models import Scenario
from pytest_httpchain.validation import DiagnosticCode, check_scenario
from tests.unit.helpers import BEYOND_RECURSION_LIMIT, nested

C = DiagnosticCode
_STAGE = {"name": "s", "request": {"url": "https://x.test/"}, "response": [{"verify": {"status": 200}}]}


def _check(stages, **top):
    data = {"stages": stages, **top}
    return check_scenario(Scenario.model_validate(data), data)


@pytest.mark.parametrize(
    ("stages", "conflict"),
    [
        # A fixture used only in stage A and a same-named parametrize parameter
        # used only in stage B never coexist (M12).
        pytest.param(
            [
                {**_STAGE, "name": "a", "fixtures": ["token"]},
                {**_STAGE, "name": "b", "parametrize": [{"individual": {"token": [1, 2]}}], "request": {"url": "https://x.test/{{ token }}"}},
            ],
            False,
            id="across-stages",
        ),
        pytest.param([{**_STAGE, "fixtures": ["token"], "substitutions": [{"vars": {"token": "x"}}]}], True, id="same-stage"),
    ],
)
def test_fixture_var_conflict_is_scoped_per_stage(stages, conflict):
    codes = {d.code for d in _check(stages)}
    assert (DiagnosticCode.FIXTURE_CONFLICT in codes) is conflict


@pytest.mark.parametrize(
    ("substitutions", "expected"),
    [
        # Scenario-level substitutions resolve before any stage runs, so an
        # unavailable name is a guaranteed crash at initialization (M13).
        pytest.param([{"vars": {"a": "{{ missing }}"}}], "Undefined variable(s) in scenario-level 'substitutions' templates: ['missing']", id="undefined"),
        pytest.param([{"vars": {"base": "https://x.test"}}, {"vars": {"url": "{{ base }}/a"}}], None, id="prior-entry"),
        # Entries resolve strictly in order, each before its own vars land, so
        # a forward or same-entry reference crashes like an undefined one.
        pytest.param([{"vars": {"a": "{{ b }}"}}, {"vars": {"b": "hello"}}], "before the substitution step that defines them: ['b']", id="forward-entry"),
        pytest.param([{"vars": {"a": "x", "b": "{{ a }}"}}], "before the substitution step that defines them: ['a']", id="same-entry"),
        # `functions` kwargs are passed to wrap_function raw — never rendered.
        pytest.param([{"functions": {"tok": {"name": "somemod:make_token", "kwargs": {"payload": "literal {{ not_a_var }} text"}}}}], None, id="function-kwargs"),
        # Templated function import names ARE rendered at seed time.
        pytest.param([{"vars": {"mod": "x"}}, {"functions": {"f": "{{ mod }}:fn"}}], None, id="function-name-prior-entry"),
        pytest.param([{"functions": {"f": "{{ missing_mod }}:fn"}}], "templates: ['missing_mod']", id="function-name-undefined"),
    ],
)
def test_scenario_substitution_references(substitutions, expected):
    found = [d for d in _check([_STAGE], substitutions=substitutions) if d.code == DiagnosticCode.SCENARIO_UNDEFINED_VAR]
    if expected is None:
        assert found == []
    else:
        assert [(d.severity, d.location) for d in found] == [("error", "substitutions")], found
        assert expected in found[0].message


def test_substitution_referencing_foreach_param_is_flagged():
    """Stage substitutions resolve before any foreach iteration variable exists,
    so referencing a foreach parameter there is undefined — even though the
    request (resolved per iteration) may reference it fine (M11). The phase is
    the whole explanation: `wid` IS defined a few lines below, just not yet when
    substitutions resolve, so the message must name the phase."""
    diags = _check(
        [
            {
                **_STAGE,
                "substitutions": [{"vars": {"derived": "{{ wid }}-x"}}],
                "parallel": {"foreach": [{"individual": {"wid": [1, 2]}}]},
                "request": {"url": "https://x.test/{{ wid }}"},
            }
        ]
    )
    undefined = [(d.location, d.message) for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR]
    assert undefined == [("stages[0].substitutions", "Stage 's': substitutions references potentially undefined variable(s): ['wid']")]


def test_undefined_names_are_reported_per_phase():
    """Two phases referencing different undefined names are two findings, each
    pointing at its own phase — not one bag naming the stage."""
    diags = _check([{**_STAGE, "request": {"url": "https://x.test/{{ nope_req }}"}, "response": [{"verify": {"status": 200, "expressions": ["{{ nope_resp }}"]}}]}])
    undefined = {d.location: d.message for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR}
    assert set(undefined) == {"stages[0].request", "stages[0].response"}, undefined
    assert "nope_req" in undefined["stages[0].request"]
    assert "nope_resp" in undefined["stages[0].response"]


@pytest.mark.parametrize("status", ["{{ nope }}", ["{{ nope }}", 304], "{{ [nope, 304] }}"], ids=["whole", "list-entry", "list-rendered"])
def test_status_templates_are_checked_in_every_form(status):
    """A status list entry is read as the whole field is: the runtime renders
    both against the response step's scope."""
    diags = _check([{**_STAGE, "response": [{"verify": {"status": status}}]}])
    undefined = [(d.location, d.message) for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR]
    assert undefined == [("stages[0].response", "Stage 's': response references potentially undefined variable(s): ['nope']")]


@pytest.mark.parametrize(
    "jmespath",
    [
        pytest.param({"id": "{{ nope }}"}, id="value"),
        pytest.param({"tags": ["{{ nope }}"]}, id="array-entry"),
        pytest.param({"id": {"gt": "{{ nope }}"}}, id="matcher-key"),
        pytest.param({"meta": {"eq": {"page": "{{ nope }}"}}}, id="eq-object-member"),
    ],
)
def test_jmespath_templates_are_checked_in_the_response_step_scope(jmespath):
    """``verify.jmespath`` values render in the response step's scope, as
    every verify field's do, wherever in the value the template sits."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": jmespath}}]}])
    assert [(d.code, d.location, d.message) for d in diags] == [
        (DiagnosticCode.UNDEFINED_VAR, "stages[0].response", "Stage 's': response references potentially undefined variable(s): ['nope']")
    ]


def test_jmespath_templates_see_what_a_response_step_sees():
    """The response namespace and a prior step's save are in scope; a later
    step's save is a forward reference, as the runtime would find it."""
    response = [
        {"save": {"jmespath": {"owner": "data.owner"}}},
        {"verify": {"jmespath": {"data.id": "{{ owner }}", "code": "{{ response.status }}", "next": "{{ later }}"}}},
        {"save": {"jmespath": {"later": "data.next"}}},
    ]
    diags = _check([{**_STAGE, "response": response}])
    assert [(d.code, d.location, d.message) for d in diags] == [
        (DiagnosticCode.FORWARD_REF, "stages[0].response", "Stage 's': response step references 'later' before the save that produces it — steps resolve in order")
    ]


@pytest.mark.parametrize("key", ["'{{ nope }}'", '"{{ nope }}"'], ids=["string-literal", "quoted-field-name"])
def test_jmespath_key_is_never_a_reference(key):
    """A key is never rendered: a template in one that compiles as JMESPath (a
    string literal, a quoted field name) is HTTPCHAIN029's, not a reference to
    an undefined name, and it is evaluated as written, not sent."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": {key: "x"}}}]}])
    assert [(d.code, d.location, d.message) for d in diags] == [
        (
            DiagnosticCode.TEMPLATE_IN_KEY,
            "stages[0].response[0].verify.jmespath",
            f"Key {key!r} contains a template expression, but a verify.jmespath key is never rendered — JMESPath evaluates it as written. "
            "Write the template in the value the key maps to.",
        )
    ]


def test_key_named_jmespath_elsewhere_is_sent_literally():
    """Only a verify step's jmespath holds expressions: a request body object
    under keys that happen to read verify.jmespath is sent as any other."""
    request = {"url": "http://server/x", "method": "POST", "body": {"json": {"verify": {"jmespath": {"{{ k }}": 1}}}}}
    diags = _check([{**_STAGE, "request": request}])
    assert [(d.code, d.message) for d in diags] == [
        (
            DiagnosticCode.TEMPLATE_IN_KEY,
            "Key '{{ k }}' contains a template expression, but only values are substituted — the key is sent literally. "
            "Move the dynamic part into the value, or build the object in a user function.",
        )
    ]


@pytest.mark.parametrize("jmespath", [{"id": 1}, {"id": None}, {"id": {"ne": None}}], ids=["value", "null", "matcher"])
def test_jmespath_alone_is_an_assertion(jmespath):
    """HTTPCHAIN006: a verify step with only jmespath asserts something."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": jmespath}}]}])
    assert diags == []


@pytest.mark.parametrize(
    ("matcher", "contradicts"),
    [
        pytest.param({"contains": "x", "not_contains": "x"}, True, id="same-substring"),
        pytest.param({"contains": [1, {"a": 2}], "not_contains": [1.0, {"a": 2}]}, True, id="equal-as-json"),
        # null is an operand: an array must both hold and lack a null.
        pytest.param({"contains": None, "not_contains": None}, True, id="both-null"),
        pytest.param({"contains": "{{ response.status }}", "not_contains": "{{ response.status }}"}, True, id="same-template"),
        pytest.param({"contains": True, "not_contains": 1}, False, id="true-is-not-1"),
        pytest.param({"contains": "1", "not_contains": 1}, False, id="text-is-not-a-number"),
        pytest.param({"contains": None}, False, id="not_contains-unset"),
        pytest.param({"matches": "^A", "not_matches": "^a"}, False, id="different-patterns"),
    ],
)
def test_jmespath_contains_contradiction(matcher, contradicts):
    """HTTPCHAIN007 for a jmespath matcher: contains and not_contains the same
    JSON value, as the check compares them, can never both hold."""
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": {"items": matcher}}}]}])
    assert [(d.code, d.location) for d in diags] == ([(DiagnosticCode.CONTAINS_CONTRADICTION, "stages[0].response[0].verify.jmespath.items")] if contradicts else [])


def test_empty_jmespath_asserts_nothing():
    diags = _check([{**_STAGE, "response": [{"verify": {"jmespath": {}}}]}])
    assert [(d.code, d.message) for d in diags] == [
        (DiagnosticCode.NOOP_VERIFY, "Stage 's': verify step asserts nothing (no status, headers, jmespath, expressions, user functions, or body checks)")
    ]


def test_dataflow_locations_are_indexed_json_paths():
    """`Diagnostic.location` is documented as a machine-routable address, so an
    unnamed stage must still produce a usable one (it used to be "")."""
    diags = _check([{"request": {"url": "https://x.test/{{ nope }}"}, "response": [{"verify": {"status": 200}}]}])
    assert all(d.location for d in diags), [d for d in diags if not d.location]
    assert {d.location for d in diags if d.code == DiagnosticCode.UNDEFINED_VAR} == {"stages[0].request"}


def test_reserved_marker_name_is_diagnostic_not_crash():
    """pytest's MarkGenerator raises AttributeError for underscore-prefixed
    names; the validator must report INVALID_MARKER, not blow up (only
    ValueError/SyntaxError used to be caught)."""
    diags = _check([{**_STAGE, "marks": ["_foo"]}])
    assert any(d.code == DiagnosticCode.INVALID_MARKER and d.severity == "error" for d in diags), diags


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        # Template references (extract_template_variables): the name at the bottom is still found.
        pytest.param({"request": {"url": "https://x.test/", "params": {"p": nested("{{ nowhere }}", BEYOND_RECURSION_LIMIT)}}}, C.UNDEFINED_VAR, id="reference"),
        # Templated keys (029) are searched in the raw JSON.
        pytest.param({"request": {"url": "https://x.test/", "params": {"p": nested({"{{ k }}": 1}, BEYOND_RECURSION_LIMIT)}}}, C.TEMPLATE_IN_KEY, id="templated-key"),
        # The schema meta-check never descends into `enum`, so this reaches the directive search (028).
        pytest.param(
            {"response": [{"verify": {"body": {"schema": {"enum": [nested(1, BEYOND_RECURSION_LIMIT)], "properties": {"a": {"$include": "a.json"}}}}}}]},
            C.SCHEMA_SCENARIO_DIRECTIVE,
            id="schema-directive",
        ),
        # contains_template decides that parametrize values resolve at collection (025).
        pytest.param({"parametrize": [{"individual": {"p": [nested("{{ 1 }}", BEYOND_RECURSION_LIMIT)]}}]}, C.PARAMETRIZE_COLLECTION_RESOLUTION, id="parametrize-template"),
    ],
)
def test_checks_reach_values_of_any_depth(fields, expected):
    """Every walk over scenario values is iterative. The recursive ones crashed
    `validate` and collection with a RecursionError on a value a few hundred
    levels deep, which loads fine."""
    assert expected in {d.code for d in _check([{**_STAGE, **fields}])}
