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
