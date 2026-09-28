import pytest

from tests.integration.helpers import stage


@pytest.mark.parametrize("body", ["json", "form", "text", "xml", "base64", "graphql", "files", "multipart"])
def test_body_reaches_the_server(run_scenario, body):
    """Each scenario's own verify steps check what the echo endpoint received —
    for multipart `files`, the field names, filenames and content sizes."""
    result = run_scenario(f"body_types/test_{body}_body.http.json", "body_types/upload_a.txt", "body_types/upload_b.bin")
    result.assert_outcomes(passed=1)


def test_stage_multipart_content_type_delimits_the_parts(run_scenario):
    """A stage's own multipart Content-Type is the one sent. Its boundary
    delimits the parts, and one naming none (``multipart/form-data`` written
    out of habit) gets the body's: sent as written, it left the server no
    boundary to find the parts by, and it saw no fields."""
    stages = [
        stage(
            name,
            "/echo/multipart",
            request={"method": "POST", "headers": {"Content-Type": content_type}, "body": {"multipart": {"fields": {"a": "1"}}}},
            response=[{"verify": {"status": 200, "jmespath": {"form.a": ["1"]}}}],
        )
        for name, content_type in [("no_boundary", "multipart/form-data"), ("own_boundary", 'multipart/form-data; boundary="my bound"')]
    ]
    result = run_scenario({"stages": stages})

    result.assert_outcomes(passed=2)
