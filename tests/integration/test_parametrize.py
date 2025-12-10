def test_parametrize_individual(pytester):
    """Test stage parametrize with individual values"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("parametrize/test_parametrize_individual.http.json")
    result = pytester.runpytest("-s")
    # 3 parametrized test cases
    result.assert_outcomes(errors=0, failed=0, passed=3)


def test_parametrize_combinations(pytester):
    """Test stage parametrize with combinations"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("parametrize/test_parametrize_combinations.http.json")
    result = pytester.runpytest("-s")
    # 2 parametrized test cases
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_parametrize_multiple(pytester):
    """Test multiple parametrize steps (cartesian product)"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("parametrize/test_parametrize_multiple.http.json")
    result = pytester.runpytest("-s")
    # 2 x 2 = 4 parametrized test cases
    result.assert_outcomes(errors=0, failed=0, passed=4)
