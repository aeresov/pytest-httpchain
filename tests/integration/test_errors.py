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


def test_connection_refused(pytester):
    """Test connection refused error when server is not running"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_connection_refused.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*Connection refused*"])


def test_invalid_hostname(pytester):
    """Test error handling for invalid hostname (DNS resolution failure)"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_invalid_hostname.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)


def test_malformed_json_save(pytester):
    """Test error handling when trying to save from malformed JSON response"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_malformed_json_save.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*not valid JSON*"])


def test_malformed_json_schema(pytester):
    """Test error handling when validating malformed JSON against schema"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_malformed_json_schema.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*not valid JSON*"])
