def test_save_jmespath(run_scenario):
    """Test JMESPath extraction from response"""
    result = run_scenario("save/test_save_jmespath.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_substitutions(run_scenario):
    """Test computed substitutions"""
    result = run_scenario("save/test_save_substitutions.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_user_function(run_scenario):
    """Test user function returning dict"""
    result = run_scenario("save/test_save_user_function.http.json", "save.py")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)
