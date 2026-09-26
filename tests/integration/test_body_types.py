import pytest


@pytest.mark.parametrize("body", ["json", "form", "text", "xml", "base64", "graphql", "files"])
def test_body_reaches_the_server(run_scenario, body):
    """Each scenario's own verify steps check what the echo endpoint received —
    for multipart `files`, the field names, filenames and content sizes."""
    result = run_scenario(f"body_types/test_{body}_body.http.json", "body_types/upload_a.txt", "body_types/upload_b.bin")
    result.assert_outcomes(passed=1)
