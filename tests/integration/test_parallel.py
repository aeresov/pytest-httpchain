def test_repeat(pytester):
    """Test repeat mode with max_concurrency"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("parallel/test_repeat.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_foreach_individual(pytester):
    """Test foreach with individual parameter values"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("parallel/test_foreach_individual.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_foreach_combinations(pytester):
    """Test foreach with parameter combinations"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("parallel/test_foreach_combinations.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)
