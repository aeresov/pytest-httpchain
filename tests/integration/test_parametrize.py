import pytest

from tests.integration.helpers import named


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("individual", 3),
        ("combinations", 2),
        ("multiple", 4),  # 2 x 2: several parametrize steps form a cartesian product
        # `combinations` given as one template resolves at collection and is
        # fed back through CombinationsParameter: the model only ever saw the
        # template string, so without that round trip the first combination's
        # keys would name the parameters of all of them. Each case verifies its
        # own expected_name, so a dropped key fails rather than passes.
        ("templated_combinations", 2),
    ),
)
def test_parametrize(run_scenario, scenario, passed):
    run_scenario(f"parametrize/test_parametrize_{scenario}.http.json").assert_outcomes(passed=passed)


def test_parametrize_templated_combinations_ragged_fails_collection(run_scenario):
    """Combinations that disagree on their keys are rejected by name at
    collection — not with a bare KeyError naming neither the index nor the
    problem, and not by silently dropping the extra key."""
    result = run_scenario("parametrize/test_parametrize_templated_combinations_ragged.http.json", args="--collect-only")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*different parameters*"])
