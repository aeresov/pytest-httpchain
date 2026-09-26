import json

import pytest

from tests.integration.helpers import named


@pytest.mark.parametrize(
    ("scenario", "outcomes"),
    named(
        ("skip_scenario", {"skipped": 1}),
        ("skip_stage", {"passed": 2, "skipped": 1}),
        ("xfail", {"xfailed": 1}),
        # An expected failure does not abort the chain: the next stage runs.
        ("xfail_continues", {"xfailed": 1, "passed": 1}),
        # The first stage fails; the always_run one after it still runs.
        ("always_run", {"failed": 1, "passed": 1}),
        # always_run templates see fixtures and saves after an abort: the
        # template-true cleanup runs, the template-false one skips like any
        # stage after an abort.
        ("always_run_template", {"failed": 1, "passed": 2, "skipped": 1}),
        # ... and stage fixtures and parametrize parameters: fixture_cleanup
        # runs, param_cleanup[true] runs, param_cleanup[false] skips.
        ("always_run_scope", {"failed": 1, "passed": 2, "skipped": 1}),
        # ... and scenario substitutions, even when a fixture error aborted the
        # chain before any stage body initialized the context.
        ("always_run_before_init", {"errors": 1, "passed": 1}),
        # Only evaluated once a stage has failed: in a healthy chain the
        # would-crash template never runs.
        ("always_run_lazy", {"passed": 2}),
    ),
)
def test_marks(run_scenario, scenario, outcomes):
    run_scenario(f"marks/test_{scenario}.http.json").assert_outcomes(**outcomes)


def test_always_run_template_error(run_scenario):
    """A broken always_run template fails the stage with a clear message."""
    result = run_scenario("marks/test_always_run_template_error.http.json")
    result.assert_outcomes(failed=2)
    result.stdout.fnmatch_lines(["*Failed to evaluate always_run template*"])


def _run_with_marks(pytester, marks):
    """Run the xfail(False) example with its stage marks replaced by ``marks``."""
    pytester.copy_example("conftest.py")
    scenario_path = pytester.copy_example("marks/test_xfail_false_condition.http.json")
    data = json.loads(scenario_path.read_text())
    data["stages"][0]["marks"] = [marks]
    scenario_path.write_text(json.dumps(data))
    return pytester.runpytest("-s")


@pytest.mark.parametrize(
    "marks",
    [
        pytest.param('xfail(False, reason="disabled condition")', id="positional-bool"),
        pytest.param('xfail(condition=False, reason="disabled condition")', id="kwarg-bool"),
        pytest.param('xfail("False", reason="disabled string condition")', id="positional-string"),
        pytest.param('xfail(condition="False", reason="disabled string condition")', id="kwarg-string"),
    ],
)
def test_inactive_xfail_aborts_chain(pytester, marks):
    """An INACTIVE xfail — falsy condition, in either the positional or the
    condition= kwarg spelling pytest honors — means pytest reports the stage
    as a genuine failure, so the carrier must abort the chain too: an inactive
    xfail must not smuggle failures past the abort machinery."""
    _run_with_marks(pytester, marks).assert_outcomes(failed=1, skipped=1)


@pytest.mark.parametrize(
    "marks",
    [
        # pytest activates xfail when ANY condition is truthy.
        pytest.param('xfail(True, False, reason="one truthy condition")', id="multi-condition"),
        # String conditions are pytest's to evaluate; a true one is active.
        pytest.param('xfail("True", reason="active string condition")', id="string"),
    ],
)
def test_active_xfail_does_not_abort(pytester, marks):
    """An active xfail's failure is expected and must not abort the chain
    (matching test_xfail_continues for the plain form)."""
    _run_with_marks(pytester, marks).assert_outcomes(passed=1, xfailed=1)
