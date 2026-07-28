def test_template_uuid(run_scenario):
    """Test uuid4() generation"""
    result = run_scenario("templates/test_template_uuid.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_expressions(run_scenario):
    """Test Python expressions in templates"""
    result = run_scenario("templates/test_template_expressions.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_type_preservation(run_scenario):
    """Test complete templates preserve type (int, dict, etc.)"""
    result = run_scenario("templates/test_template_type_preservation.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_helpers(run_scenario):
    """Test exists() and get() helper functions"""
    result = run_scenario("templates/test_template_helpers.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)
