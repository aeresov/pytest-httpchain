def test_json_body(run_scenario):
    """Test JSON body in POST request"""
    result = run_scenario("body_types/test_json_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_form_body(run_scenario):
    """Test URL-encoded form body"""
    result = run_scenario("body_types/test_form_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_text_body(run_scenario):
    """Test raw text body"""
    result = run_scenario("body_types/test_text_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_xml_body(run_scenario):
    """Test XML body"""
    result = run_scenario("body_types/test_xml_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_base64_body(run_scenario):
    """Test base64-encoded binary data"""
    result = run_scenario("body_types/test_base64_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_graphql_body(run_scenario):
    """Test GraphQL query with variables"""
    result = run_scenario("body_types/test_graphql_body.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_files_body(run_scenario):
    """Multipart file upload end to end: the httpx `files` kwarg wiring, field
    names, filenames, and content sizes all reach the server."""
    result = run_scenario("body_types/test_files_body.http.json", "body_types/upload_a.txt", "body_types/upload_b.bin")
    result.assert_outcomes(errors=0, failed=0, passed=1)
