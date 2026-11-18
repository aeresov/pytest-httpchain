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


TWO_OPERATIONS = "query Me { me { id } } query Users { users { id } }"


def test_graphql_operation_name_reaches_the_server(run_scenario):
    """A graphql body's operation_name is sent as operationName, written
    out or rendered from a template."""
    stages = [
        stage(
            name,
            "/echo/json",
            request={"method": "POST", "body": {"graphql": {"query": TWO_OPERATIONS, "operation_name": operation_name}}},
            response=[{"verify": {"status": 200, "jmespath": {"received.operationName": "Users", "received.query": TWO_OPERATIONS}}}],
        )
        for name, operation_name in [("written", "Users"), ("templated", "{{ operation }}")]
    ]
    result = run_scenario({"substitutions": [{"vars": {"operation": "Users"}}], "stages": stages})
    result.assert_outcomes(passed=2)


def test_graphql_operation_name_a_template_renders_to_no_operation_fails_the_stage(run_scenario):
    """Checked once rendered, as a written one is at load: the stage fails
    before anything is sent."""
    stages = [stage("unknown", "/echo/json", request={"method": "POST", "body": {"graphql": {"query": TWO_OPERATIONS, "operation_name": "{{ operation }}"}}})]
    result = run_scenario({"substitutions": [{"vars": {"operation": "Them"}}], "stages": stages})
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*operation_name 'Them' names no operation in the query (it defines 'Me', 'Users')*"])
