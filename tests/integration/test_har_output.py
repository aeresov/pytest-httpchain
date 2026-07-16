"""End-to-end HAR export (M52).

The har_writer unit tests cover the serialization shape; this exercises the
full plugin path: running a scenario with ``--output-dir`` must drop a `.har`
file per executed stage, and that file must parse as valid HAR JSON.
"""

import json


def test_har_file_written(pytester):
    har_dir = pytester.path / "har_out"

    pytester.copy_example("conftest.py")
    pytester.copy_example("verify/test_verify_status.http.json")
    result = pytester.runpytest("-s", "--output-dir", str(har_dir))

    # Sanity: the scenario itself passes (2 stages = 2 test methods).
    result.assert_outcomes(errors=0, failed=0, passed=2)

    # A HAR file must have been written under the output dir.
    har_files = list(har_dir.glob("*.har"))
    assert har_files, f"expected a .har file under {har_dir}, found none"

    # It must parse as JSON with the canonical HAR top-level shape.
    har = json.loads(har_files[0].read_text(encoding="utf-8"))
    assert "log" in har
    assert isinstance(har["log"]["entries"], list)
    assert har["log"]["entries"], "HAR log must contain at least one entry"


def test_parallel_stage_har_contains_every_iteration(pytester):
    """A parallel stage's HAR must hold one entry per iteration, not a single
    arbitrary iteration presented as the stage's only exchange. The report
    section title must also say which of how many iterations it shows."""
    har_dir = pytester.path / "har_out"

    pytester.copy_example("conftest.py")
    pytester.copy_example("parallel/test_repeat.http.json")
    result = pytester.runpytest("-s", "--httpchain-output-dir", str(har_dir))

    result.assert_outcomes(errors=0, failed=0, passed=1)

    har_files = list(har_dir.glob("*.har"))
    assert len(har_files) == 1
    entries = json.loads(har_files[0].read_text(encoding="utf-8"))["log"]["entries"]
    assert len(entries) == 5  # repeat: 5
    assert all(e["response"]["status"] == 200 for e in entries)


def test_timeout_still_produces_report_and_har(pytester):
    """A timed-out request previously vanished: no request section, no HAR.
    Now the request that was on the wire is reported and the HAR carries a
    status-0 entry for it."""
    har_dir = pytester.path / "har_out"

    pytester.copy_example("conftest.py")
    (pytester.path / "test_timeout.http.json").write_text(
        json.dumps(
            {
                "stages": [
                    {
                        "name": "times_out",
                        "fixtures": ["server"],
                        "request": {"url": "{{ server }}/delay/2", "timeout": 0.2},
                        "response": [{"verify": {"status": 200}}],
                    }
                ]
            }
        )
    )
    # Subprocess, not in-process: pytester's in-process mode restores
    # sys.modules between runs, which breaks httpx's lazily-cached
    # httpcore-exception mapping (isinstance against classes from a stale
    # httpcore module) — the timeout then surfaces as an unmapped
    # httpcore.ReadTimeout without request info. A real pytest run (fresh
    # interpreter) always gets the mapped httpx.ReadTimeout.
    result = pytester.runpytest_subprocess("-s", "--httpchain-output-dir", str(har_dir))

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*HTTP Request*", "*GET*/delay/2*"])

    har_files = list(har_dir.glob("*.har"))
    assert len(har_files) == 1
    entries = json.loads(har_files[0].read_text(encoding="utf-8"))["log"]["entries"]
    assert len(entries) == 1
    assert entries[0]["response"]["status"] == 0
    assert entries[0]["request"]["url"].endswith("/delay/2")


def test_parallel_failure_report_labels_shown_iteration(pytester):
    """The report shows one exchange for a parallel stage; the section title
    must say it is one of many, not present it as the stage's only request."""
    pytester.copy_example("conftest.py")
    pytester.copy_example("errors/test_parallel_failure.http.json")
    result = pytester.runpytest("-s")

    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*HTTP Request (failing of 3 parallel iterations)*"])
