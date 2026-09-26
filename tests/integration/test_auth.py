import pytest


@pytest.mark.parametrize("scenario", ["scenario_auth", "request_auth", "auth_from_fixture"])
def test_auth(run_scenario, scenario):
    run_scenario(f"auth/test_{scenario}.http.json", "auth.py").assert_outcomes(passed=1)


def test_request_auth_failure_fails_stage_cleanly(run_scenario):
    """A raising request-level auth function becomes a clean stage failure that
    aborts the chain — not a raw traceback bypassing the abort machinery (the
    scenario-level twin lives in the lazy-init suite; this is request_builder's
    per-request branch)."""
    result = run_scenario("auth/test_request_auth_failure.http.json", "auth.py")
    result.assert_outcomes(failed=1, skipped=1)
    result.stdout.fnmatch_lines(["*Failed to configure authentication*"])
