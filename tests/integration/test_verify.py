from tests.integration.conftest import run_scenario


def test_verify_status(pytester):
    """Test status code verification"""
    result = run_scenario(pytester, "verify/test_verify_status.http.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_headers(pytester):
    """Test header verification"""
    result = run_scenario(pytester, "verify/test_verify_headers.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_expressions(pytester):
    """Test bool expression verification"""
    result = run_scenario(pytester, "verify/test_verify_expressions.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_user_function(pytester):
    """Test user function returning bool"""
    result = run_scenario(pytester, "verify/test_verify_user_function.http.json", "verify.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_schema(pytester):
    """Test JSON schema validation"""
    result = run_scenario(pytester, "verify/test_verify_body_schema.http.json", "verify/schema.json")
    # 2 stages = 2 test methods
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_verify_body_contains(pytester):
    """Test body contains/not_contains"""
    result = run_scenario(pytester, "verify/test_verify_body_contains.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_verify_body_matches(pytester):
    """Test body regex matching"""
    result = run_scenario(pytester, "verify/test_verify_body_matches.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)
