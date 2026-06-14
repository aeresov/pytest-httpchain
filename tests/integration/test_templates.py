from tests.integration.conftest import run_scenario


def test_template_uuid(pytester):
    """Test uuid4() generation"""
    result = run_scenario(pytester, "templates/test_template_uuid.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_expressions(pytester):
    """Test Python expressions in templates"""
    result = run_scenario(pytester, "templates/test_template_expressions.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_type_preservation(pytester):
    """Test complete templates preserve type (int, dict, etc.)"""
    result = run_scenario(pytester, "templates/test_template_type_preservation.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_template_helpers(pytester):
    """Test exists() and get() helper functions"""
    result = run_scenario(pytester, "templates/test_template_helpers.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)
