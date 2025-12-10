def test_json_body(pytester):
    """Test JSON body in POST request"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("body_types/test_json_body.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_form_body(pytester):
    """Test URL-encoded form body"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("body_types/test_form_body.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_text_body(pytester):
    """Test raw text body"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("body_types/test_text_body.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_xml_body(pytester):
    """Test XML body"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("body_types/test_xml_body.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_base64_body(pytester):
    """Test base64-encoded binary data"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("body_types/test_base64_body.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_graphql_body(pytester):
    """Test GraphQL query with variables"""
    pytester.copy_example("auth.py")
    pytester.copy_example("conftest.py")
    pytester.copy_example("body_types/test_graphql_body.http.json")
    result = pytester.runpytest("-s")
    result.assert_outcomes(errors=0, failed=0, passed=1)
