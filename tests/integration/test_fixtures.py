import pytest

from tests.integration.helpers import named


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("fixture_injection", 1),
        ("fixture_dict", 1),
        ("fixture_factory", 1),  # a callable fixture (factory pattern)
        # Scenario-level fixtures reach every stage, deduplicated against the
        # stage's own fixtures.
        ("scenario_fixtures", 2),
    ),
)
def test_fixture_values_reach_templates(run_scenario, scenario, passed):
    run_scenario(f"fixtures/test_{scenario}.http.json").assert_outcomes(passed=passed)
