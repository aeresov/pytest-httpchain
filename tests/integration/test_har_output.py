"""End-to-end HAR export (M52).

The har_writer unit tests cover the serialization shape; this exercises the
full plugin path: running a scenario with ``--httpchain-output-dir`` must drop a `.har`
file per executed stage, and that file must parse as valid HAR JSON.
"""

import json
from datetime import datetime

import pytest

from tests.integration.helpers import HAR_ARGS, har_entries, named, stage


@pytest.fixture
def har_dir(pytester):
    return pytester.path / "har_out"


def test_har_file_written_per_stage(run_scenario, har_dir):
    result = run_scenario("verify/test_verify_status.http.json", args=HAR_ARGS)

    result.assert_outcomes(passed=2)
    per_file = sorted([e["response"]["status"] for e in json.loads(p.read_text(encoding="utf-8"))["log"]["entries"]] for p in har_dir.glob("*.har"))
    assert per_file == [[200], [400]]


def test_har_preserves_same_name_cookies_with_different_paths(run_scenario, har_dir):
    """A valid response may scope the same cookie name to several paths;
    exporting it must not raise httpx.CookieConflict and drop the HAR file."""
    result = run_scenario({"stages": [stage("scoped_cookies", "/scoped-cookies")]}, args=HAR_ARGS)

    result.assert_outcomes(passed=1)
    cookies = har_entries(har_dir)[0]["response"]["cookies"]
    assert {(cookie["name"], cookie["value"], cookie["path"]) for cookie in cookies} == {
        ("session", "root", "/"),
        ("session", "admin", "/admin"),
    }


def test_har_form_repeats_are_separate_scalar_params(run_scenario, har_dir):
    form = {"method": "POST", "body": {"form": {"tag": ["one", "two"], "empty": ""}}}
    result = run_scenario({"stages": [stage("repeated_form", "/echo/form", request=form)]}, args=HAR_ARGS)

    result.assert_outcomes(passed=1)
    assert har_entries(har_dir)[0]["request"]["postData"]["params"] == [
        {"name": "tag", "value": "one"},
        {"name": "tag", "value": "two"},
        {"name": "empty", "value": ""},
    ]


def test_har_records_url_query_merged_with_params(run_scenario, har_dir):
    """The HAR shows the URL that went out: its own query merged with params,
    where httpx's params= used to replace it and category=books never reached
    the server."""
    request = {"params": {"sort": "price", "limit": 10}}
    result = run_scenario({"stages": [stage("merged_query", "/search?category=books&sort=name", request=request)]}, args=HAR_ARGS)

    result.assert_outcomes(passed=1)
    [entry] = har_entries(har_dir)
    assert entry["request"]["url"].endswith("/search?category=books&sort=price&limit=10")
    assert entry["request"]["queryString"] == [
        {"name": "category", "value": "books"},
        {"name": "sort", "value": "price"},
        {"name": "limit", "value": "10"},
    ]
    # /search echoes the two keys it reads.
    assert json.loads(entry["response"]["content"]["text"]) == {"category": "books", "sort": "price", "results": []}


def test_url_reaches_the_server_as_written(run_scenario, har_dir):
    """The rendered URL used to be re-validated into pydantic's HttpUrl and sent
    WHATWG-normalized: this encoded path-traversal probe went out as ``/ok``,
    got a 200, and the stage expecting the server to refuse it failed."""
    not_found = [{"verify": {"status": 404}}]
    result = run_scenario({"stages": [stage("encoded_dot_segment", "/static/%2e%2e/ok", response=not_found)]}, args=HAR_ARGS)

    result.assert_outcomes(passed=1)
    [entry] = har_entries(har_dir)
    assert entry["request"]["url"].endswith("/static/%2e%2e/ok")


def test_parallel_stage_har_contains_every_iteration(run_scenario, har_dir):
    """A parallel stage's HAR must hold one entry per iteration, not a single
    arbitrary iteration presented as the stage's only exchange."""
    result = run_scenario("parallel/test_repeat.http.json", args=HAR_ARGS)

    result.assert_outcomes(passed=1)
    assert [e["response"]["status"] for e in har_entries(har_dir)] == [200] * 5  # repeat: 5


@pytest.mark.slow
def test_timeout_still_produces_report_and_har(run_scenario, har_dir):
    """A timed-out request previously vanished: no request section, no HAR.
    Now the request that was on the wire is reported and the HAR carries a
    status-0 entry for it."""
    # Subprocess, not in-process: pytester's in-process mode restores
    # sys.modules between runs, which breaks httpx's lazily-cached
    # httpcore-exception mapping (isinstance against classes from a stale
    # httpcore module) — the timeout then surfaces as an unmapped
    # httpcore.ReadTimeout without request info. A real pytest run (fresh
    # interpreter) always gets the mapped httpx.ReadTimeout.
    result = run_scenario({"stages": [stage("times_out", "/delay/2", request={"timeout": 0.2})]}, args=HAR_ARGS, subprocess=True)

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*HTTP Request*", "*GET*/delay/2*"])
    [entry] = har_entries(har_dir)
    assert entry["response"]["status"] == 0
    assert entry["request"]["url"].endswith("/delay/2")


def test_parallel_failure_report_labels_shown_iteration(run_scenario):
    """The report shows one exchange for a parallel stage; the section title
    must say it is one of many, not present it as the stage's only request."""
    result = run_scenario("errors/test_parallel_failure.http.json")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*HTTP Request (failing of 3 parallel iterations)*"])


def test_stage_failing_before_request_inherits_nothing(run_scenario, har_dir):
    """A stage that fails before any request runs (bad substitution template)
    must not inherit the previous parallel stage's exchanges: no stale
    'parallel iterations' label, no HAR file under its nodeid."""
    scenario = {
        "stages": [
            stage("parallel_ok", parallel={"repeat": 3}),
            stage("fails_pre_request", substitutions=[{"vars": {"boom": "{{ no_such_name }}"}}]),
        ]
    }
    result = run_scenario(scenario, args=HAR_ARGS)

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.no_fnmatch_line("*failing of 3 parallel iterations*")
    har_files = [p.name for p in har_dir.glob("*.har")]
    assert len(har_files) == 1, har_files  # only the parallel stage wrote one
    assert "parallel_ok" in har_files[0]


@pytest.mark.slow
def test_timeout_after_passing_stage_shows_no_stale_response(run_scenario):
    """A timed-out stage's report must not pair its request with the previous
    stage's response."""
    scenario = {"stages": [stage("passes"), stage("times_out", "/delay/2", request={"timeout": 0.2})]}
    # Subprocess for the same httpx/pytester interaction documented in
    # test_timeout_still_produces_report_and_har.
    result = run_scenario(scenario, subprocess=True)

    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(["*HTTP Request*", "*GET*/delay/2*"])
    result.stdout.no_fnmatch_line("*HTTP Response*")


def test_har_entries_carry_real_start_times(run_scenario, har_dir):
    """startedDateTime must be each request's actual start, not export time:
    a rate-limited stage (3 iterations at 2/sec) spreads real starts over
    roughly a second, while export-time fabrication packs every entry within
    milliseconds of the stage's end."""
    result = run_scenario("parallel/test_rate_limit_slow.http.json", args=HAR_ARGS)
    result.assert_outcomes(passed=1)

    entries = har_entries(har_dir)
    assert len(entries) == 3
    times = sorted(datetime.fromisoformat(e["startedDateTime"]) for e in entries)
    spread = (times[-1] - times[0]).total_seconds()
    assert spread >= 0.4, f"start times span only {spread}s — fabricated at export time?"


def test_multipart_upload_degrades_in_har_and_report(run_scenario, har_dir):
    """A multipart (files) body is a streaming httpx request that httpx never
    buffers and request_content does not read back; the HAR and report paths
    must degrade to 'body not captured' instead of erroring — previously the
    whole HAR file was silently dropped and the request section showed a
    formatting error."""
    result = run_scenario(
        "body_types/test_files_body.http.json",
        "body_types/upload_a.txt",
        "body_types/upload_b.bin",
        args=(*HAR_ARGS, "-rA"),
    )

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*HTTP Request*", "*Streaming body*not captured*"])
    result.stdout.no_fnmatch_line("*Error formatting*")
    [entry] = har_entries(har_dir)
    # -1 is HAR's "unknown size": the streaming body is not captured.
    assert entry["request"]["bodySize"] == -1
    assert "postData" not in entry["request"]
    assert entry["response"]["status"] == 200


# httpx builds a redirect follow-up that keeps the method from the original's
# body stream and never reads it; the report and HAR used to present that body
# as a consumed streaming upload.
_POST_JSON = {"method": "POST", "body": {"json": {"k": "v"}}}


@pytest.mark.parametrize(
    ("name", "path", "request_"),
    named(
        # The follow-up re-sends the GET's own empty body stream.
        ("get_302", "/redirect-ok", {}),
        # A 302 turns the POST into a GET, which drops the body. httpx buffers
        # that rebuilt empty body, so this row guards the POST body staying off it.
        ("post_302", "/redirect-post/302?to=/ok", _POST_JSON),
    ),
)
def test_redirect_follow_up_without_body_reports_empty_body(run_scenario, har_dir, name, path, request_):
    result = run_scenario({"stages": [stage(name, path, request=request_)]}, args=(*HAR_ARGS, "-rA"))

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*HTTP Request (after 1 redirect)*"])
    result.stdout.no_fnmatch_line("*Streaming body*")
    _, follow_up = (entry["request"] for entry in har_entries(har_dir))
    assert follow_up["method"] == "GET"
    assert follow_up["bodySize"] == 0
    assert "postData" not in follow_up


def test_redirect_follow_up_replaying_body_reports_it(run_scenario, har_dir):
    """A 307 re-POSTs the body to the target: the follow-up's HAR entry
    carries it just as the first hop's does, and the report shows it."""
    result = run_scenario({"stages": [stage("post_307", "/redirect-post/307?to=/echo/json", request=_POST_JSON)]}, args=(*HAR_ARGS, "-rA"))

    result.assert_outcomes(passed=1)
    # Inside the request section: the response echoes the body too, indented deeper.
    result.stdout.fnmatch_lines(["*HTTP Request (after 1 redirect)*", '  "k": "v"', "*HTTP Response (after 1 redirect)*"])
    hop, follow_up = (entry["request"] for entry in har_entries(har_dir))
    assert follow_up["method"] == "POST"
    assert json.loads(follow_up["postData"]["text"]) == {"k": "v"}
    assert follow_up["postData"] == hop["postData"]
    assert follow_up["bodySize"] == hop["bodySize"]
