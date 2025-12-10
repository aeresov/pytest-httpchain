def test_verify_status(pytester):
    """Test status code verification"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_status.http.json")
    result = pytester.runpytest("-s")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_headers(pytester):
    """Test header verification"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_headers.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_expressions(pytester):
    """Test bool expression verification"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_expressions.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_user_function(pytester):
    """Test user function returning bool"""
    pytester.copy_example("auth.py")
    pytester.copy_example("verify.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_user_function.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_schema(pytester):
    """Test JSON schema validation"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/schema.json")
    pytester.copy_example("verify/test_verify_body_schema.http.json")
    result = pytester.runpytest("-s")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_body_contains(pytester):
    """Test body contains/not_contains"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_body_contains.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_matches(pytester):
    """Test body regex matching"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_body_matches.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)
