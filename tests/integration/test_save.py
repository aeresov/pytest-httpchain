def test_save_jmespath(pytester):
    """Test JMESPath extraction from response"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("save/test_save_jmespath.http.json")
    result = pytester.runpytest("-s")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_substitutions(pytester):
    """Test computed substitutions"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("save/test_save_substitutions.http.json")
    result = pytester.runpytest("-s")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_save_user_function(pytester):
    """Test user function returning dict"""
    pytester.copy_example("auth.py")
    pytester.copy_example("save.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("save/test_save_user_function.http.json")
    result = pytester.runpytest("-s")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)
