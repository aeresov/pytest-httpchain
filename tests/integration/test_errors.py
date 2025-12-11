def test_verify_failure(pytester):
    """Test verification failure reports clearly"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_verify_failure.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)


def test_timeout_error(pytester):
    """Test request timeout handling"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_timeout_error.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)


def test_expression_failure(pytester):
    """Test expression verification failure"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_expression_failure.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)


def test_header_failure(pytester):
    """Test header verification failure"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_header_failure.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)


def test_parallel_failure(pytester):
    """Test parallel execution failure handling"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_parallel_failure.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)
