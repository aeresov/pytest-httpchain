from tests.integration.conftest import run_scenario


def test_save_jmespath(pytester):
    """Test JMESPath extraction from response"""
    result = run_scenario(pytester, "save/test_save_jmespath.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_substitutions(pytester):
    """Test computed substitutions"""
    result = run_scenario(pytester, "save/test_save_substitutions.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_user_function(pytester):
    """Test user function returning dict"""
    result = run_scenario(pytester, "save/test_save_user_function.http.json", "save.py")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)
