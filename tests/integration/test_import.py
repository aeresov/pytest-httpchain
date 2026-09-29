"""``pytest-httpchain import curl`` against the example server: the scenario
it writes runs, its placeholders filled in from the environment."""

import json

from typer.testing import CliRunner

from pytest_httpchain.cli import app

# Commands as an API's docs would show them, against a host the example
# server stands in for.
COMMANDS = """
# Basic credentials, which the scenario reads from API_PASSWORD.
curl -u user:pass https://api.example.test/answer

curl -X POST https://api.example.test/echo/json \\
  -H 'Content-Type: application/json' \\
  -d '{"name": "Ann", "tags": ["a", "{{ literal }}"]}'

curl https://api.example.test/echo/form -d 'a=1' --data-urlencode 'b=x y'

curl 'https://api.example.test/search?category=shoes&sort=price' -H 'Accept: application/json'

curl -F 'title=Report' -F 'note=<note.txt;type=text/plain' https://api.example.test/echo/multipart

# A JSON Schema posted to a registry: its $ref is the document's, which the
# scenario file's loader must not resolve.
$ curl https://api.example.test/echo/json --json '{"schema": {"$ref": "#/definitions/a"}, "definitions": {"a": {"type": "string"}}}'

# The body a here-document gives, which -d @- reads.
curl -X POST https://api.example.test/echo/json -H 'Content-Type: application/json' -d @- <<'EOF'
{"name": "Bo"}
EOF
"""


def test_imported_curl_commands_run(run_scenario, pytester, monkeypatch):
    """The scenario an import writes runs as it is, pointed at the server
    that answers: its base URL is the one line to change, as it would be to
    run it against another environment."""
    output = pytester.path / "imported.json"
    result = CliRunner().invoke(app, ["import", "curl", "-o", str(output), COMMANDS])
    assert result.exit_code == 0, result.output
    assert "  API_PASSWORD  the password of user 'user' (stage get_answer)" in result.stderr
    assert "note: the scenario reads these files, a relative path from the scenario's own directory: note.txt" in result.stderr
    assert "note: Stage 'post_echo_json_2': its JSON body is written as text" in result.stderr

    scenario = json.loads(output.read_text())
    assert scenario["client"]["base_url"] == "https://api.example.test"
    scenario["client"]["base_url"] = "{{ env('HTTPCHAIN_EXAMPLE_API_ROOT') }}"
    scenario["marks"] = ["usefixtures('api_root')"]
    (pytester.path / "note.txt").write_text("hello")
    monkeypatch.setenv("API_PASSWORD", "pass")
    # What the echo endpoints received is what the commands sent, a `{{`
    # included (the expectation escapes it too, being a template's text).
    received = {
        "get_answer": {"answer": 42},
        "post_echo_json": {"received": {"eq": {"name": "Ann", "tags": ["a", "\\{{ literal }}"]}}},
        "post_echo_form": {"form": {"eq": {"a": "1", "b": "x y"}}},
        "get_search": {"category": "shoes", "sort": "price"},
        "post_echo_multipart": {"form": {"eq": {"title": ["Report"], "note": ["hello"]}}},
        # (A "$ref" key in the expectation would be resolved too.)
        "post_echo_json_2": {'received.schema."$ref"': "#/definitions/a", "received.definitions": {"eq": {"a": {"type": "string"}}}},
        "post_echo_json_3": {"received": {"eq": {"name": "Bo"}}},
    }
    for stage in scenario["stages"]:
        stage["response"].append({"verify": {"jmespath": received[stage["name"]]}})

    run = run_scenario(scenario)
    run.assert_outcomes(passed=7)


def test_an_unset_secret_fails_its_stage(run_scenario, pytester, monkeypatch):
    """A secret's placeholder left unset fails the stage rather than send the
    request without it: a query parameter too, which the scenario keeps in
    the URL, since a params value rendered to null would be sent empty."""
    commands = "curl -H 'X-API-Key: k-1' https://api.example.test/headers\ncurl 'https://api.example.test/search?category=shoes&access_token=t-1'"
    result = CliRunner().invoke(app, ["import", "curl", commands])
    assert result.exit_code == 0, result.output
    scenario = json.loads(result.stdout)
    assert scenario["stages"][1]["request"]["url"] == "/search?category=shoes&access_token={{ quote(access_token) }}"
    scenario["client"]["base_url"] = "{{ env('HTTPCHAIN_EXAMPLE_API_ROOT') }}"
    scenario["marks"] = ["usefixtures('api_root')"]
    monkeypatch.setenv("X_API_KEY", "k-1")
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)

    run = run_scenario(scenario)
    run.assert_outcomes(passed=1, failed=1)
    run.stdout.fnmatch_lines(["*quote() takes text or bytes, not None*"])
