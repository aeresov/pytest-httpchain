"""Test that stage-level seed variables work correctly."""


def test_stage_vars_override_scenario_vars(pytester):
    """Test that stage vars can override scenario-level vars."""
    pytester.copy_example("stage_vars/conftest.py")
    pytester.copy_example("stage_vars/test_stage_vars.http.json")

    result = pytester.runpytest("-v")

    # Test should pass, proving that:
    # 1. Stage vars override scenario vars (initial_value)
    # 2. Stage vars persist to subsequent stages (stage_only_var)
    # 3. Saved variables from previous stages are accessible (echo_result)
    result.assert_outcomes(passed=2)


def test_stage_vars_fixture_conflict_validation(pytester):
    """Test that stage vars cannot conflict with fixture names."""
    pytester.copy_example("stage_vars/conftest.py")
    pytester.copy_example("stage_vars/test_stage_vars_conflict.http.json")

    result = pytester.runpytest("-v")

    # Should fail due to validation error
    result.assert_outcomes(failed=1)
    assert "Variable name 'server' conflicts with fixture name" in result.stdout.str()
