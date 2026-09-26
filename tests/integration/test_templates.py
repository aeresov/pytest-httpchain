import pytest


# uuid4(); Python expressions; complete templates keep their type (int, dict,
# ...); the exists()/get() helpers.
@pytest.mark.parametrize("scenario", ["uuid", "expressions", "type_preservation", "helpers"])
def test_template_features(run_scenario, scenario):
    run_scenario(f"templates/test_template_{scenario}.http.json").assert_outcomes(passed=1)
