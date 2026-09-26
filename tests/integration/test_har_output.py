"""End-to-end HAR export (M52).

The har_writer unit tests cover the serialization shape; this exercises the
full plugin path: running a scenario with ``--httpchain-output-dir`` must drop a `.har`
file per executed stage, and that file must parse as valid HAR JSON.
"""

import json
from datetime import datetime

import pytest

from tests.integration.helpers import har_entries, stage

# Relative to the pytester dir, which is the CWD of both in-process and
# subprocess runs.
HAR_ARGS = ("-s", "--httpchain-output-dir", "har_out")


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
    """A multipart (files) body is a streaming httpx request whose bytes are
    consumed on send; the HAR and report paths must degrade to 'body not
    captured' instead of erroring — previously the whole HAR file was silently
    dropped and the request section showed a formatting error."""
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
    # -1 is HAR's "unknown size": the streaming body is gone after the send.
    assert entry["request"]["bodySize"] == -1
    assert "postData" not in entry["request"]
    assert entry["response"]["status"] == 200
