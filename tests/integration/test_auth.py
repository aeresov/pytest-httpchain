from tests.integration.conftest import run_scenario


def test_scenario_auth(pytester):
    """Test scenario-level auth function"""
    result = run_scenario(pytester, "auth/test_scenario_auth.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_request_auth(pytester):
    """Test request-level auth"""
    result = run_scenario(pytester, "auth/test_request_auth.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_auth_from_fixture(pytester):
    """Test auth using fixture values"""
    result = run_scenario(pytester, "auth/test_auth_from_fixture.http.json", "auth.py")
    result.assert_outcomes(errors=0, failed=0, passed=1)
