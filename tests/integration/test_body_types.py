from tests.integration.conftest import run_scenario


def test_json_body(pytester):
    """Test JSON body in POST request"""
    result = run_scenario(pytester, "body_types/test_json_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_form_body(pytester):
    """Test URL-encoded form body"""
    result = run_scenario(pytester, "body_types/test_form_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_text_body(pytester):
    """Test raw text body"""
    result = run_scenario(pytester, "body_types/test_text_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_xml_body(pytester):
    """Test XML body"""
    result = run_scenario(pytester, "body_types/test_xml_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_base64_body(pytester):
    """Test base64-encoded binary data"""
    result = run_scenario(pytester, "body_types/test_base64_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_graphql_body(pytester):
    """Test GraphQL query with variables"""
    result = run_scenario(pytester, "body_types/test_graphql_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)
