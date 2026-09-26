"""Outcomes only pytest classifies — fixture errors around a stage body, a
strict XPASS — must abort the chain like a failing stage body does."""

import pytest


@pytest.mark.parametrize(
    ("scenario", "outcomes"),
    [
        pytest.param("errors/test_fixture_setup_abort", {"errors": 1, "skipped": 1}, id="fixture-setup-error"),
        pytest.param("errors/test_fixture_teardown_abort", {"errors": 1, "passed": 1, "skipped": 1}, id="fixture-teardown-error"),
        pytest.param("marks/test_strict_xpass_abort", {"failed": 1, "skipped": 1}, id="strict-xpass"),
    ],
)
def test_pytest_classified_failure_aborts_chain(run_scenario, scenario, outcomes):
    run_scenario(f"{scenario}.http.json").assert_outcomes(**outcomes)
