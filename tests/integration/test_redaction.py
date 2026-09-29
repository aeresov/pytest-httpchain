"""Credential redaction in what a run prints and exports.

The rules themselves are pinned in tests/unit/test_redaction.py; this runs
them through the plugin: the ini options reach the report sections, the
failure messages (through the carrier) and, only when asked, the HAR file.
"""

import json

import pytest

from tests.integration.helpers import HAR_ARGS, har_entries, named, stage

# Every secret below is spelled "s3cret-<where>", so one substring search says
# whether any of them got out.
_CREDENTIALED = {
    "params": {"access_token": "s3cret-query", "page": 2},
    "headers": {"Authorization": "Bearer s3cret-header", "Cookie": "sid=s3cret-cookie", "X-Trace": "trace-1"},
}


@pytest.mark.parametrize(
    ("name", "args", "leaks"),
    named(
        ("redacted_by_default", (), False),
        # Empty lists switch redaction off: the values are shown as sent.
        ("disabled", ("-o", "httpchain_redact_headers=", "-o", "httpchain_redact_query_params="), True),
    ),
)
def test_failing_stage_report_hides_credentials(run_scenario, name, args, leaks):
    result = run_scenario({"stages": [stage(name, "/bad", request=_CREDENTIALED)]}, args=("-s", *args))

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert ("s3cret" in output) is leaks
    if not leaks:
        assert "?access_token=[REDACTED]&page=2" in output
        assert "authorization: [REDACTED]" in output
        assert "cookie: sid=[REDACTED]" in output
        # Only the listed headers: the rest of the report is untouched.
        assert "x-trace: trace-1" in output


@pytest.mark.slow
def test_rejected_header_value_is_redacted_in_the_failure_message(run_scenario):
    """h11 refuses a header value ending in a newline (a token read from a
    file) and quotes it in its error, which heads the failure above the
    redacted request section: the quoted value is redacted the same way."""
    request = {"headers": {"Authorization": "Bearer s3cret-newline\n"}}
    # Subprocess: in-process pytester can leave httpx's exception mapping
    # undone (see test_har_output), and the unmapped error carries no request.
    result = run_scenario({"stages": [stage("token_from_file", request=request)]}, subprocess=True)

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "s3cret" not in output
    assert "HTTP request failed: Illegal header value b'[REDACTED]'" in output
    assert "authorization: [REDACTED]" in output


def test_relative_url_failure_message_is_redacted(run_scenario):
    """A URL a template renders relative, without a client.base_url, fails
    before anything is sent, so no report section follows the message: the
    message itself hides the credential, by the configured list."""
    scenario = {
        "substitutions": [{"vars": {"path": "/ok?sig=s3cret-sig&page=2"}}],
        "stages": [{"name": "relative", "request": {"url": "{{ path }}"}}],
    }
    result = run_scenario(scenario, args=("-s", "-o", "httpchain_redact_query_params=sig"))

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "s3cret" not in output
    assert "Request URL '/ok?sig=[REDACTED]&page=2' is relative" in output


def test_configured_list_replaces_the_default(run_scenario):
    """A list names every header to redact: Authorization is shown once the
    list leaves it out."""
    request = {"headers": {"Authorization": "Bearer visible", "X-Tenant-Secret": "s3cret-tenant"}}
    result = run_scenario({"stages": [stage("custom_list", "/bad", request=request)]}, args=("-s", "-o", "httpchain_redact_headers=X-Tenant-Secret"))

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "x-tenant-secret: [REDACTED]" in output
    assert "authorization: Bearer visible" in output


def test_redirect_location_query_is_redacted(run_scenario):
    """A redirect the stage does not follow shows its Location, which is how
    OAuth hands a token over: the URL's credentials go, the rest stays."""
    request = {"method": "POST", "allow_redirects": False, "params": {"to": "/ok?state=keep&access_token=s3cret-location"}}
    result = run_scenario({"stages": [stage("oauth_callback", "/redirect-post/302", request=request)]})

    result.assert_outcomes(failed=1)
    # Only the header line: the request's own `to` parameter, which is not a
    # listed name, and the server's HTML redirect body still carry the URL.
    [location] = [line for line in result.stdout.lines if line.startswith("location: ")]
    assert location.endswith("/ok?state=keep&access_token=[REDACTED]")


def test_set_cookie_mismatch_message_hides_the_values(run_scenario):
    """The failure message echoes the header it compared: both sides keep the
    cookie's name and attributes and lose the value, as the report does."""
    response = [{"verify": {"headers": {"Set-Cookie": "session=s3cret-guess; Path=/"}}}]
    result = run_scenario({"stages": [stage("cookie_mismatch", "/scoped-cookies", response=response)]})

    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "expected session=[REDACTED]; Path=/, got session=[REDACTED]; Path=/, session=[REDACTED]; Path=/admin" in output
    assert "set-cookie: session=[REDACTED]; Path=/admin" in output
    for value in ("s3cret-guess", "session=root", "session=admin"):
        assert value not in output


@pytest.mark.parametrize(
    ("name", "args", "redacted"),
    named(
        # HAR is typically replayed, which needs the real values.
        ("har_unredacted_by_default", (), False),
        ("har_redact_on", ("-o", "httpchain_har_redact=true"), True),
    ),
)
def test_har_is_redacted_only_when_enabled(run_scenario, pytester, name, args, redacted):
    result = run_scenario({"stages": [stage(name, "/scoped-cookies", request=_CREDENTIALED)]}, args=(*HAR_ARGS, "-rA", *args))

    result.assert_outcomes(passed=1)
    # The report is redacted either way.
    assert "s3cret" not in result.stdout.str()
    [entry] = har_entries(pytester.path / "har_out")
    har_text = json.dumps(entry)
    har_request, har_response = entry["request"], entry["response"]
    headers = {h["name"]: h["value"] for h in har_request["headers"]}
    if redacted:
        assert "s3cret" not in har_text
        assert har_request["url"].endswith("/scoped-cookies?access_token=[REDACTED]&page=2")
        assert har_request["queryString"] == [{"name": "access_token", "value": "[REDACTED]"}, {"name": "page", "value": "2"}]
        assert (headers["authorization"], headers["cookie"]) == ("[REDACTED]", "sid=[REDACTED]")
        assert har_request["cookies"] == [{"name": "sid", "value": "[REDACTED]"}]
        assert [(c["name"], c["value"]) for c in har_response["cookies"]] == [("session", "[REDACTED]")] * 2
    else:
        assert "[REDACTED]" not in har_text
        assert headers["authorization"] == "Bearer s3cret-header"
        assert har_request["url"].endswith("/scoped-cookies?access_token=s3cret-query&page=2")
        assert [c["value"] for c in har_response["cookies"]] == ["root", "admin"]


def test_followed_redirect_is_redacted_in_report_and_har(run_scenario, pytester):
    """The report shows the final hop's request, and the HAR every hop, each
    Location in its redirectURL: the credential is redacted in all of them."""
    request = {"method": "POST", "params": {"to": "/ok?token=s3cret-hop"}}
    args = (*HAR_ARGS, "-rA", "-o", "httpchain_har_redact=true")
    result = run_scenario({"stages": [stage("followed", "/redirect-post/302", request=request)]}, args=args)

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*HTTP Request (after 1 redirect)*", "GET http://*/ok?token=[[]REDACTED]"])
    hop, follow_up = har_entries(pytester.path / "har_out")
    assert hop["response"]["redirectURL"] == "/ok?token=[REDACTED]"
    assert follow_up["request"]["url"].endswith("/ok?token=[REDACTED]")
