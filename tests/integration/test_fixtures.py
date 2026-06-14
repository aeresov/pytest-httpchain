from tests.integration.conftest import run_scenario


def test_fixture_injection(pytester):
    """Test fixture values in stage context"""
    result = run_scenario(pytester, "fixtures/test_fixture_injection.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_fixture_dict(pytester):
    """Test fixture providing dict values"""
    result = run_scenario(pytester, "fixtures/test_fixture_dict.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_fixture_factory(pytester):
    """Test callable fixture (factory pattern)"""
    result = run_scenario(pytester, "fixtures/test_fixture_factory.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_scenario_fixtures(pytester):
    """Test scenario-level fixtures injected into all stages, deduplicated against stage fixtures"""
    result = run_scenario(pytester, "fixtures/test_scenario_fixtures.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=2)
