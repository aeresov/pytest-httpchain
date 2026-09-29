import pytest

from tests.integration.helpers import named


@pytest.mark.parametrize(
    ("scenario", "passed"),
    named(
        ("repeat", 1),
        # M50: `repeat: N` must fire N requests, not one. The first stage POSTs
        # to the /counter endpoint N times in parallel; a final stage verifies
        # the count equals N (the `server` fixture resets it at setup).
        ("repeat_counter", 2),
        ("foreach_individual", 1),
        ("foreach_combinations", 1),
        # M2: calls_per_sec must construct the limiter and run to completion.
        ("rate_limit", 1),
    ),
)
def test_parallel_stage_passes(run_scenario, scenario, passed):
    run_scenario(f"parallel/test_{scenario}.http.json").assert_outcomes(passed=passed)


def test_rate_limit_exceeded(run_scenario):
    """Exceeding max_rate_limit_delay fails cleanly, not with a raw traceback (M2)."""
    result = run_scenario("parallel/test_rate_limit_exceeded.http.json")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*Rate limit exceeded*"])


def test_parallel_no_partial_save(run_scenario):
    """M4: a failing parallel stage commits no saves. The first stage saves `leaked`
    in its passing iterations but fails overall (one iteration hits /bad); the
    always_run second stage confirms `leaked` never reached the global context."""
    result = run_scenario("parallel/test_parallel_no_partial_save.http.json")
    # stage 1 fails (an iteration hits /bad -> 400); stage 2 (always_run) passes
    # only because `leaked` was NOT committed. Without M4 it would be failed=2.
    result.assert_outcomes(failed=1, passed=1)


# A stage tolerating the failures of an endpoint that fails every 3rd request
# (6 of 9 pass), a stage whose every request fails at a min_success_ratio of 0,
# a stage reading what both saved, and a stage whose thresholds the same 6 of
# 9 do not meet.
THRESHOLDS = "parallel/test_parallel_thresholds.http.json"

# The failure of the stage whose thresholds are not met: every one listed, the
# failed iterations after them, whichever three got the 500s.
THRESHOLDS_NOT_MET = [
    "*2 parallel thresholds not met:",
    "*  1. min_success_ratio: 0.666667 (6 of 9 iterations passed), below the limit 0.9",
    "*  2. max_p95_ms: * ms, above the limit 0.001 ms",
    "*3 failed iterations:",
    "*  iteration *: Status code doesn't match: expected 200, got 500",
    "*  iteration *: Status code doesn't match: expected 200, got 500",
    "*  iteration *: Status code doesn't match: expected 200, got 500",
    "*- Parallel Summary -*",
    "Iterations:     9 (6 passed, 3 failed, 0 cancelled)",
    "Success ratio:  0.6667",
    "Wall time:      * ms",
    "Throughput:     * completed iterations/s, * passed iterations/s",
    "Latency (ms):   min *, mean *, p50 *, p95 *, p99 *, max *",
    "Thresholds:     min_success_ratio 0.9: not met, 0.666667 (6 of 9 iterations passed)",
    "                max_p95_ms 0.001: not met, * ms",
    # The first failed iteration's exchange, as a stage failing at one shows it.
    "*- HTTP Request (failing of 9 parallel iterations) -*",
]

# The summary of the stage that ran on after its failures, and passed.
TOLERATED = [
    "*- Parallel Summary -*",
    "Iterations:     9 (6 passed, 3 failed, 0 cancelled)",
    "Success ratio:  0.6667",
    "Thresholds:     min_success_ratio 0.6: met, 0.666667 (6 of 9 iterations passed)",
    "                max_p95_ms 60000: met, * ms",
    "*- HTTP Request (last of 9 parallel iterations) -*",
]


def test_parallel_summary_and_thresholds(run_scenario):
    """The tolerant stage sends all nine requests and passes, saving its
    stats and the six ids, each at its iteration's index, for a later stage
    to check. The stage only measuring passes with none passed, and still
    saves its ids, null in each iteration's place, as validate says, its
    report showing a failure's exchange. The stricter stage fails naming
    both thresholds. Every parallel stage's
    report has a summary, and only a parallel stage's."""
    result = run_scenario(THRESHOLDS, args=("-rA",))
    result.assert_outcomes(passed=3, failed=1)
    result.stdout.fnmatch_lines(THRESHOLDS_NOT_MET)
    result.stdout.fnmatch_lines(TOLERATED)
    # With none passed, a failed iteration's exchange is the one shown.
    result.stdout.fnmatch_lines(
        ["*- Parallel Summary -*", "Iterations:     2 (0 passed, 2 failed, 0 cancelled)", "Success ratio:  0.0000", "*- HTTP Request (failing of 2 parallel iterations) -*"]
    )
    assert result.stdout.str().count("Parallel Summary") == 3


@pytest.mark.slow
def test_parallel_summary_survives_xdist(run_scenario):
    """A worker ships its report sections to the controller, which prints them."""
    result = run_scenario(THRESHOLDS, args=("-n", "2", "--dist", "loadscope", "-rA"), subprocess=True)
    result.assert_outcomes(passed=3, failed=1)
    result.stdout.fnmatch_lines(THRESHOLDS_NOT_MET)
    result.stdout.fnmatch_lines(TOLERATED)


def test_collect_saves_cleans_up_every_iteration(run_scenario):
    """With parallel.collect_saves, the stage creating four resources commits
    every iteration's id and name, as lists in iteration order, which a later
    stage checks against the server. Once a stage has failed the chain, the
    always_run stage whose foreach goes through the ids deletes every one, and
    none is left. Merged, only the last iteration's id would survive: the
    foreach would have no list to go through."""
    result = run_scenario("parallel/test_collect_saves.http.json")
    result.assert_outcomes(passed=4, failed=1)
    result.stdout.fnmatch_lines(["FAILED *::test 2 - fails*"])


@pytest.mark.slow
def test_rate_limiter_threads_not_leaked(run_scenario):
    """Each rate-limited stage execution used to construct a pyrate-limiter
    Limiter and never dispose it — and each Limiter owns a leaker daemon thread
    that keeps itself alive forever. The limiter must be closed once the
    stage's iterations are done."""
    import threading
    import time

    result = run_scenario("parallel/test_rate_limit.http.json")
    result.assert_outcomes(passed=1)

    def leakers() -> list[str]:
        return [t.name for t in threading.enumerate() if "pyratelimiter" in t.name.lower().replace(" ", "")]

    # Limiter.close() signals the leaker thread, which only notices on its next
    # wake — pyrate-limiter's leak interval is 10s — so the deadline must span
    # one full wake cycle. An unclosed (pre-fix) leaker is immortal: its own
    # reference keeps it alive forever, so it is still running at 15s.
    deadline = time.monotonic() + 15
    while leakers() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert leakers() == [], f"leaked rate-limiter threads: {leakers()}"
