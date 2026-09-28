import pytest

from tests.integration.helpers import stage


# uuid4(); Python expressions; complete templates keep their type (int, dict,
# ...); the exists()/get() helpers.
@pytest.mark.parametrize("scenario", ["uuid", "expressions", "type_preservation", "helpers"])
def test_template_features(run_scenario, scenario):
    run_scenario(f"templates/test_template_{scenario}.http.json").assert_outcomes(passed=1)


def test_value_nested_hundreds_deep_collects_and_runs(run_scenario):
    """A `vars` value 600 levels deep loads fine. Collection used to crash with a
    RecursionError: the validator walked it recursively, two frames per level.
    The stage's template walk spent two frames per level of it as well."""
    deep = "{{ 'x' }}"
    for _ in range(600):
        deep = {"k": deep}
    run_scenario({"stages": [stage("deep", substitutions=[{"vars": {"deep": deep}}])]}).assert_outcomes(passed=1)
