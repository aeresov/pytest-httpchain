import pytest

from tests.integration.helpers import named


@pytest.mark.parametrize(
    "scenario",
    [
        "jmespath",
        "substitutions",
        "user_function",
        # Entries within ONE save step resolve strictly in order, each seeing
        # the previous entry's names (regression: an eager pre-walk evaluated
        # later entries before earlier ones' names existed).
        "substitutions_sequential",
        # Server text that LOOKS like a template ('{{ probe }}') is saved
        # literally (regression: a double template pass re-evaluated rendered
        # values, executing response-derived text as expressions).
        "preserves_template_literals",
    ],
)
def test_save_then_use_in_next_stage(run_scenario, scenario):
    run_scenario(f"save/test_save_{scenario}.http.json", "save.py").assert_outcomes(passed=2)


@pytest.mark.parametrize(
    ("scenario", "outcomes", "line"),
    named(
        # A clean stage failure that aborts the chain, like the verify-side twin.
        ("user_function_raises", {"failed": 1, "skipped": 1}, "*Error calling user function*"),
        # Rejected, not coerced.
        ("user_function_non_dict", {"failed": 1}, "*must return dict*"),
        # Compiles, but errors at search time: still no raw traceback.
        ("jmespath_runtime_error", {"failed": 1}, "*Error saving variable*"),
        ("substitutions_error", {"failed": 1}, "*Error processing substitutions*"),
    ),
)
def test_save_fails_cleanly(run_scenario, scenario, outcomes, line):
    result = run_scenario(f"save/test_save_{scenario}.http.json", "save.py")
    result.assert_outcomes(**outcomes)
    result.stdout.fnmatch_lines([line])
