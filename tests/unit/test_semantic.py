"""Unit tests for `validation.check_scenario` (validation/semantic.py) on
in-memory scenarios. The file-based, end-to-end validator tests live in
test_validation.py."""

import pytest

from pytest_httpchain.models import Scenario
from pytest_httpchain.validation import DiagnosticCode, check_scenario

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
