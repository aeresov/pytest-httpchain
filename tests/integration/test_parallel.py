import pytest

from tests.integration.conftest import run_scenario


def test_repeat(pytester):
    """Test repeat mode with max_concurrency"""
    result = run_scenario(pytester, "parallel/test_repeat.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_repeat_counter(pytester):
    """M50: a parallel `repeat: N` stage must actually fire N requests, not one.

    The first stage POSTs to the thread-safe /counter endpoint N times in
    parallel; a final non-parallel stage GETs the resulting count and verifies
    it equals N. The `server` fixture resets the counter at setup, so a silent
    single execution would yield count==1 and fail the verification.
    """
    result = run_scenario(pytester, "parallel/test_repeat_counter.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=2)


def test_foreach_individual(pytester):
    """Test foreach with individual parameter values"""
    result = run_scenario(pytester, "parallel/test_foreach_individual.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_foreach_combinations(pytester):
    """Test foreach with parameter combinations"""
    result = run_scenario(pytester, "parallel/test_foreach_combinations.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_rate_limit(pytester):
    """calls_per_sec must construct the limiter and run to completion (M2)."""
    result = run_scenario(pytester, "parallel/test_rate_limit.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)


def test_rate_limit_exceeded(pytester):
    """Exceeding max_rate_limit_delay fails cleanly, not with a raw traceback (M2)."""
    result = run_scenario(pytester, "parallel/test_rate_limit_exceeded.http.json")
    result.assert_outcomes(errors=0, failed=1, passed=0)
    result.stdout.fnmatch_lines(["*Rate limit exceeded*"])


def test_parallel_no_partial_save(pytester):
    """M4: a failing parallel stage commits no saves. The first stage saves `leaked`
    in its passing iterations but fails overall (one iteration hits /bad); the
    always_run second stage confirms `leaked` never reached the global context."""
    result = run_scenario(pytester, "parallel/test_parallel_no_partial_save.http.json")
    # stage 1 fails (an iteration hits /bad -> 400); stage 2 (always_run) passes
    # only because `leaked` was NOT committed. Without M4 it would be failed=2.
    result.assert_outcomes(errors=0, failed=1, passed=1)


@pytest.mark.slow
def test_rate_limiter_threads_not_leaked(pytester):
    """Each rate-limited stage execution used to construct a pyrate-limiter
    Limiter and never dispose it — and each Limiter owns a leaker daemon thread
    that keeps itself alive forever. The limiter must be closed once the
    stage's iterations are done."""
    import threading
    import time

    result = run_scenario(pytester, "parallel/test_rate_limit.http.json")
    result.assert_outcomes(errors=0, failed=0, passed=1)

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
