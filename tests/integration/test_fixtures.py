def test_fixture_injection(run_scenario):
    """Test fixture values in stage context"""
    result = run_scenario("fixtures/test_fixture_injection.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_fixture_dict(run_scenario):
    """Test fixture providing dict values"""
    result = run_scenario("fixtures/test_fixture_dict.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_fixture_factory(run_scenario):
    """Test callable fixture (factory pattern)"""
    result = run_scenario("fixtures/test_fixture_factory.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_scenario_fixtures(run_scenario):
    """Test scenario-level fixtures injected into all stages, deduplicated against stage fixtures"""
    result = run_scenario("fixtures/test_scenario_fixtures.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=2)
