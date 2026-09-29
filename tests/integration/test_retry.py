"""Stage ``retry`` end to end: polling jobs on the example server until they
are done, what the report and the HAR show of the attempts, and a request
retried over a real refused connection.

The attempt loop's rules (backoff, the ``on`` filters, the saves discarded,
cancellation) are pinned in tests/unit/test_carrier.py.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.integration.helpers import HAR_ARGS

POLL_JOB = "retry/test_retry_poll_job.http.json"


def _poll_jobs(polls: list[int], attempts: int, *, parallel: bool = True) -> dict[str, Any]:
    """A scenario starting one job per entry of ``polls``, each done after
    that many polls, then polling them with ``attempts`` attempts: all at
    once, one iteration per job, or without ``parallel`` the first alone. One
    ``api_root`` server serves both stages, so the jobs outlive the first."""
    poll: dict[str, Any] = {
        "name": "poll",
        "retry": {"attempts": attempts, "delay": 0.01},
        "request": {"url": "/jobs/{{ job_id }}" if parallel else "/jobs/{{ job_ids[0] }}"},
        "response": [{"verify": {"status": 200, "jmespath": {"status": "done"}}}],
    }
    if parallel:
        poll["parallel"] = {"foreach": [{"individual": {"job_id": "{{ job_ids }}"}}], "max_concurrency": len(polls)}
    return {
        "marks": ["usefixtures('api_root')"],
        "client": {"base_url": "{{ env('HTTPCHAIN_EXAMPLE_API_ROOT') }}"},
        "stages": [
            {
                "name": "submit",
                "parallel": {"foreach": [{"individual": {"polls": polls}}], "collect_saves": True},
                "request": {"method": "POST", "url": "/jobs", "body": {"json": {"polls": "{{ polls }}"}}},
                "response": [{"verify": {"status": 202}}, {"save": {"jmespath": {"job_ids": "id"}}}],
            },
            poll,
        ],
    }


def _stage_har(har_dir: Path, stage_name: str) -> list[dict[str, Any]]:
    """The entries of the HAR file of the stage named ``stage_name``."""
    [path] = [path for path in har_dir.glob("*.har") if stage_name in path.name]
    return json.loads(path.read_text(encoding="utf-8"))["log"]["entries"]


def _polled(entries: list[dict[str, Any]]) -> list[int]:
    """How many polls the job had had, by each HAR entry's response."""
    return [json.loads(entry["response"]["content"]["text"])["polled"] for entry in entries]


def test_polls_until_the_job_is_done(run_scenario):
    """Three polls: two pending, one done. Each attempt starts from a fresh
    context, and the stage keeps the saves of the one that passed, which a
    later stage reads."""
    run_scenario(POLL_JOB).assert_outcomes(passed=3)


def test_report_shows_the_last_attempt(run_scenario):
    result = run_scenario(POLL_JOB, args=("-rP",))
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["*HTTP Request (attempt 3 of 5)*", "*HTTP Response (attempt 3 of 5)*"])


def test_har_records_every_attempt(run_scenario, pytester):
    """Every attempt went on the wire, so each is an entry of the stage's HAR,
    in order, and the uuid4() in its header was drawn anew for each."""
    run_scenario(POLL_JOB, args=HAR_ARGS).assert_outcomes(passed=3)
    entries = _stage_har(pytester.path / "har_out", "wait_for_job")
    assert [json.loads(entry["response"]["content"]["text"])["status"] for entry in entries] == ["pending", "pending", "done"]
    attempt_ids = {header["value"] for entry in entries for header in entry["request"]["headers"] if header["name"].lower() == "x-attempt-id"}
    assert len(attempt_ids) == 3


def test_last_attempt_failing_fails_the_stage(run_scenario, pytester):
    """The job needs more polls than the stage has attempts: the stage fails
    with the last attempt's failure, counting the attempts, and the report
    shows that attempt. The HAR has all three."""
    result = run_scenario(_poll_jobs([5], attempts=3, parallel=False), args=HAR_ARGS)
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            """*JMESPath 'status' doesn't match: expected "done", got "pending" (after 3 attempts)*""",
            "*HTTP Response (attempt 3 of 3)*",
        ]
    )
    assert _polled(_stage_har(pytester.path / "har_out", "poll")) == [1, 2, 3]


def test_parallel_iterations_retry_each_on_its_own(run_scenario, pytester):
    """Each iteration polls its own job until it is done, done after 1, 2
    and 3 polls: the stage passes with every iteration making the attempts
    its job needs. The HAR has them all, iteration by iteration in index
    order, and each iteration's attempts in the order they were made."""
    run_scenario(_poll_jobs([1, 2, 3], attempts=3), args=HAR_ARGS).assert_outcomes(passed=2)
    assert _polled(_stage_har(pytester.path / "har_out", "poll")) == [1, 1, 2, 1, 2, 3]


def test_parallel_iteration_out_of_attempts_fails_the_stage(run_scenario):
    result = run_scenario(_poll_jobs([1, 5], attempts=3))
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines(
        [
            """*Parallel execution failed at iteration 1: JMESPath 'status' doesn't match: expected "done", got "pending" (after 3 attempts)*""",
            "*HTTP Response (failing of 2 parallel iterations, attempt 3 of 3)*",
        ]
    )


@pytest.mark.slow
def test_refused_connection_is_retried(run_scenario):
    """A connection refused is a request failure that ``on: request``
    retries, on a real socket. Subprocess: pytester's in-process mode breaks
    httpx's mapping of httpcore's errors (see test_har_output), which would
    report the refusal as an unexpected error, never retried."""
    scenario = {
        "stages": [
            {
                "name": "refused",
                "fixtures": ["closed_port"],
                "retry": {"attempts": 3, "delay": 0.01, "on": "request"},
                "request": {"url": "http://127.0.0.1:{{ closed_port }}/"},
                "response": [{"verify": {"status": 200}}],
            }
        ]
    }
    result = run_scenario(scenario, subprocess=True)
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*HTTP connection error: * (after 3 attempts)*"])
